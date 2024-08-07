import logging
import hashlib
import os
import math
import glob
import multiprocessing
from typing import Tuple, Any, List, Set, Callable, Union, TextIO
import subprocess
import asyncio
import json
import importlib.util

import fiona
import yaml
import tqdm
import geopandas as gpd
import pandas as pd
import numpy as np
from osgeo import gdal, osr
from shapely.geometry import box
from pyproj import Transformer

# Optional imports:
# - xarray

# GDAL setups
os.environ["GDAL_DISABLE_READDIR_ON_OPEN"] = 'TRUE' # Set to avoid reading really large folders
os.environ["GDAL_NUM_THREADS"] = 'ALL_CPUS'
gdal.UseExceptions()


if (spec := importlib.util.find_spec('pyogrio')) is not None:
    GPD_ENGINE = "pyogrio"
    if int(gdal.VersionInfo()) >= 3060000 and importlib.util.find_spec('pyogrio') is not None:
        os.environ["PYOGRIO_USE_ARROW"] = "1"
else:
    GPD_ENGINE = "fiona"

if gdal.GetDriverByName("Parquet") is not None:
    # If gdal parquet library is installed, use it because its faster/better compression
    GEOMETRY_SAVE_EXTENSION = "parquet"
else:
    GEOMETRY_SAVE_EXTENSION = "gpkg"
    

class AutoRouteHandler:
    def __init__(self, 
                 yaml: str = None) -> None:
        """
        yaml may be string or dictionary
        """
        if yaml:
            self.setup(yaml)
            
        self._hash_file = '.file_metadata.json'
        self._file_hash_dict = multiprocessing.Manager().dict()
        if os.path.exists(self._hash_file):
            with open(self._hash_file, 'r') as f:
                try:
                    tmp = json.load(f)
                except json.JSONDecodeError:
                    tmp = {}
                for key, value in tmp.items():
                    self._file_hash_dict[key] = value

    def run(self) -> None:
        logging.info("Starting model run...")
        dems = []
        strms = []
        lus = []
        sim_flow_files = []
        flood_files = []
        self.test_ok()
        if self.DEM_FOLDER:
            if os.path.isdir(self.DEM_FOLDER):
                # Get all .tif files in the folder and subfolders using walkdir
                dems = {os.path.join(root, f) for root, _, files in os.walk(self.DEM_FOLDER) for f in files if f.lower().endswith((".tif", ".vrt"))}
                #dems = {os.path.join(self.DEM_FOLDER,f) for f in os.listdir(self.DEM_FOLDER) if f.lower().endswith((".tif", ".vrt"))}
            elif os.path.isfile(self.DEM_FOLDER):
                dems = {self.DEM_FOLDER}
            elif not os.path.exists(self.DEM_FOLDER):
                logging.error(f"DEM folder {self.DEM_FOLDER} does not exist. Exiting...")
                return
        else:
            if self.BUFFER_FILES:
                dems = {f for f in glob.glob(os.path.join(self.DATA_DIR, 'dems', "buffered", "*")) if f.lower().endswith((".tif", ".vrt"))}
            elif self.CROP:
                dems = {f for f in glob.glob(os.path.join(self.DATA_DIR, 'dems', "cropped", "*")) if f.lower().endswith((".tif", ".vrt"))}
            else:
                dems = glob.glob(os.path.join(self.DATA_DIR, 'dems', "*"))
        if self.EXTENT:
            dems = {dem for dem in dems if self.is_in_extent(dem, self.EXTENT)}
        if not dems:
            logging.warning(f"No DEMs found!!")
            return
        processes = min(len(dems), os.cpu_count())
        n_dems = len(dems)
        mifns = None
        with multiprocessing.Pool(processes=processes) as pool:
            if self.BUFFER_FILES and self.DEM_FOLDER:
                logging.info(f"Buffering {n_dems} DEM(s)...")
                dems = list(tqdm.tqdm(pool.imap_unordered(self.buffer_dem, dems), disable=self.DISABLE_PBAR))

            elif self.CROP and self.EXTENT and self.DEM_FOLDER:
                logging.info(f"Cropping {n_dems} DEM(s)...")
                dems = list(tqdm.tqdm(pool.imap_unordered(self.crop, dems), total=len(dems), disable=self.DISABLE_PBAR))

            if self.STREAM_NETWORK_FOLDER:
                strms_dems = self.map_dems_and_streams(dems)
                logging.info(f"Creating stream rasters for {n_dems} DEM(s)...")
                strms = tqdm.tqdm(pool.imap_unordered(self.create_strm_file, strms_dems), total=len(strms_dems), disable=self.DISABLE_PBAR) # TODO fix total
                strms = [strm for strm_list in strms for strm in strm_list if strm]
            else:
                logging.error("No stream network folder or stream name provided. Exiting...")
                return
            
            n_strms = len(strms)
            if n_strms != n_dems:
                logging.warning(f"Only {n_strms} stream files were created out of {n_dems} DEMs")

            if self.LAND_USE_FOLDER:
                logging.info(f"Creating land use rasters for {n_dems} DEM(s)...")
                lus = list(tqdm.tqdm(pool.imap_unordered(self.create_land_use, dems), 
                                     total=len(dems), disable=self.DISABLE_PBAR))

            if not self.DEM_FOLDER and self.EXTENT:
                strms = {strm for strm in strms if self.is_in_extent(strm, self.EXTENT)}
            if strms and self.SIMULATION_FLOWFILE:
                sim_flow_files =list(tqdm.tqdm(pool.imap_unordered(self.create_row_col_id_file, strms), 
                                               total=len(strms),desc='Creating row col id files', disable=self.DISABLE_PBAR))
                if self.multiprocess_data.get('SIMULATION_ID_COLUMN', False):
                    self.SIMULATION_ID_COLUMN = self.multiprocess_data['SIMULATION_ID_COLUMN']

            if self.FLOOD_FLOWFILE:
                flood_files =list(tqdm.tqdm(pool.imap_unordered(self.create_flood_flowfile, strms), 
                                            total=len(strms),desc='Creating flow files', disable=self.DISABLE_PBAR))
            
            pairs = self._zip_files(dems, strms, lus, sim_flow_files, flood_files)
            if self.AUTOROUTE or self.FLOODSPREADER:
                mifns = pool.starmap(self.create_mifn_file, pairs)
                mifns = [mifn for mifn in mifns if mifn]
                if len(mifns) != n_dems:
                    logging.warning(f"Only {len(mifns)} mifn files were created out of {n_dems} DEMs")

            if self.AUTOROUTE:
                set(tqdm.tqdm(pool.imap_unordered(self.run_autoroute, mifns), total=len(mifns), 
                              desc="Running AutoRoute", disable=self.DISABLE_PBAR))

            if self.FLOODSPREADER:
                if not self.FLOOD_FLOWFILE:
                    logging.warning("FloodSpreader requires a flood flow file. Will not run...")
                else:
                    set(tqdm.tqdm(pool.imap_unordered(self.run_floodspreader, mifns), total=len(mifns), 
                                  desc='Running FloodSpreader', disable=self.DISABLE_PBAR))
                    logging.info('FloodSpreader finished')

            if self.CLEAN_OUTPUTS:
                set(tqdm.tqdm(pool.imap_unordered(self.optimize_outputs, mifns), total=len(mifns), 
                              desc="Optimizing outputs", disable=self.DISABLE_PBAR))

            logging.info("Finished processing all inputs\n")
        
        self.save_hashes()
 
    def setup(self, yaml_file: Union[dict, str]) -> None:
        """
        Ensure working folder exists and set up. Each folder contains a folder based on the type of dem used and the stream network
        DATA_DIR
            dems
                FABDEM__v1-1
                    - 1.tif
                    - 2.tif
                    - 1_crop.tif
                other_dem__v1-1
            stream_files
                FABDEM__v1-1
                    - 1.tif
                    - 2.tif
            land_use
                FABDEM__v1-1
                    - 1.tif
            rapid_files
                FABDEM__v1-1
                        - 1.txt
            flow_files
                FABDEM__v1-1
                        - 1.txt
            vdts
                FABDEM__v1-1
                    - 1.txt
            mifns
                FABDEM__v1-1
                    - 1.txt
            meta_files
                FABDEM__v1-1
                    - 1.txt
        """
        # Read the yaml file and get variables out of it
        self.DATA_DIR = ""
        self.BUFFER_FILES = False
        self.BUFFER_DISTANCE = 0.1
        self.DEM_FOLDER = ""
        self.STREAM_NETWORK_FOLDER = ""
        self.LAND_USE_FOLDER = "" 
        self.SIMULATION_FLOWFILE = ""
        self.FLOOD_FLOWFILE: Callable = None
        self.SIMULATION_ID_COLUMN = ""
        self.SIMULATION_FLOW_COLUMN = ""
        self.EXTENT = None
        self.CROP = False
        self.OVERWRITE = False
        self.STREAM_ID = ""
        self.BASE_FLOW_COLUMN = ""
        self.MANNINGS_TABLE = ""
        self.CLEAN_OUTPUTS = False
        self.multiprocess_data = multiprocessing.Manager().dict()
        self.DISABLE_PBAR = False

        self.AUTOROUTE_PYTHON_MAIN: Callable = "" # Hidden
        self.USE_PYTHON = False
        self.AUTOROUTE = ""
        self.FLOODSPREADER = ""
        self.AUTOROUTE_CONDA_ENV = ""

        self.RAPID_Subtract_BaseFlow = False
        self.VDT = ""
        self.num_iterations = 15
        self.convert_cfs_to_cms = False
        self.x_distance = 1000
        self.q_limit = 1.1
        self.direction_distance = 1
        self.slope_distance = 1
        self.weight_angles = 0
        self.use_prev_d_4_xs = 1
        self.adjust_flow = 1
        self.Str_Limit_Val = 0
        self.UP_Str_Limit_Val = float('inf')
        self.row_start = 0
        self.row_end = float('inf')
        self.degree_manip = 0.0
        self.degree_interval = 0.0
        self.man_n = 0.01
        self.low_spot_distance = 2
        self.low_spot_is_meters = False
        self.low_spot_use_box = False
        self.box_size = 1
        self.find_flat = False
        self.low_spot_find_flat_cutoff = float('inf')
        self.run_bathymetry = False
        self.ar_bathy_file = ''
        self.bathy_alpha = 0.001
        self.bathy_method = ''
        self.bathy_x_max_depth = 0.2
        self.bathy_y_shallow = 0.2

        self.da_flow_param = ''
        self.omit_outliers = ''
        self.wse_search_dist = 10
        self.wse_threshold = 0.25
        self.wse_remove_three = False
        self.specify_depth = 0
        self.twd_factor = 1.5
        self.only_streams = False
        self.use_ar_top_widths = False
        self.flood_local = False
        self.DEPTH_MAP = ''
        self.FLOOD_MAP = ''
        self.VELOCITY_MAP = ''
        self.WSE_MAP = ''
        self.fs_bathy_file = ''
        self.fs_bathy_smooth_method = ''
        self.bathy_twd_factor = 1

        if isinstance(yaml_file, dict):
            for key, value in yaml_file.items():
                setattr(self, key, value)
        elif isinstance(yaml_file, str):
            with open(yaml_file, 'r') as f:
                data = yaml.safe_load(f)
                for key, value in data.items():
                    setattr(self, key, value)

        if not self.DATA_DIR:
            logging.warning(f'No working folder provided! Using {os.getcwd()}')
            self.DATA_DIR = os.getcwd()
        if not os.path.isabs(self.DATA_DIR):
            self.DATA_DIR = os.path.abspath(self.DATA_DIR)
        os.makedirs(self.DATA_DIR, exist_ok = True)
        #os.chdir(self.DATA_DIR)
        with open(os.path.join(self.DATA_DIR,'This is the working folder. Please delete and modify with caution.txt'), 'a') as f:
            pass

        os.makedirs(os.path.join(self.DATA_DIR,'dems'), exist_ok = True)
        os.makedirs(os.path.join(self.DATA_DIR,'dems', 'buffered'), exist_ok = True)
        os.makedirs(os.path.join(self.DATA_DIR,'dems', 'cropped'), exist_ok = True)
        os.makedirs(os.path.join(self.DATA_DIR,'stream_files'), exist_ok = True)
        os.makedirs(os.path.join(self.DATA_DIR,'land_use'), exist_ok = True)
        os.makedirs(os.path.join(self.DATA_DIR,'rapid_files'), exist_ok = True)
        os.makedirs(os.path.join(self.DATA_DIR,'flow_files'), exist_ok = True)
        os.makedirs(os.path.join(self.DATA_DIR,'vdts'), exist_ok = True)
        os.makedirs(os.path.join(self.DATA_DIR,'mifns'), exist_ok = True)
        os.makedirs(os.path.join(self.DATA_DIR,'stream_reference_files'), exist_ok=True)
        os.makedirs(os.path.join(self.DATA_DIR,'tmp'), exist_ok=True)
        os.makedirs(os.path.join(self.DATA_DIR, 'meta_files'), exist_ok=True)
        os.makedirs(os.path.join(self.DATA_DIR, 'bathymetry'), exist_ok=True)
        os.makedirs(os.path.join(self.DATA_DIR, 'curves'), exist_ok=True)

    def find_dems_in_extent(self, extent: list = None) -> List[str]:
        dems = self.find_files(self.DEM_FOLDER)
        if extent:
            dems = [dem for dem in dems if self.is_in_extent(dem, extent)]
        return dems

    def find_number_of_dems_in_extent(self) -> int:
        dems = self.find_dems_in_extent()
        return len(dems)

    def find_files(self, directory: str, type: str = '*.tif') -> List[str]:
        tif_files = []
        for root, _, _ in os.walk(directory):
            tif_files.extend(glob.glob(os.path.join(root, type)))
        return tif_files

    def is_in_extent(self, ds: Any,extent: list = None) -> bool:
        if extent is None:
            return True
        if isinstance(ds, str):
            ds = self.open_w_gdal(ds)
        if not ds: return False

        minx, miny, maxx,maxy = self.get_ds_extent(ds)
        ds = None
        return self._isin(minx, miny, maxx, maxy, extent)
    
    def _isin(self, minx: float, miny: float, maxx: float, maxy: float, extent: list) -> bool:
        minx1, miny1, maxx1, maxy1 = extent 
        if (minx1 <= maxx and maxx1 >= minx and miny1 <= maxy and maxy1 >= miny):
            return True
        return False
    
    def buffer_dem(self, dem: str) -> str:
        ds = self.open_w_gdal(dem)
        if not ds: return
        projection = ds.GetProjection()
        no_data_value = ds.GetRasterBand(1).GetNoDataValue()

        # try:
        #     srs = osr.SpatialReference(wkt=ds.GetProjection())
        #     if srs.IsProjected():
        #         units = srs.GetLinearUnitsName()
        #     else:
        #         units = srs.GetAngularUnitsName()
        #     if 'degree' in units:
        #         buffer_distance = 0.1 # Buffer by 0.1 degrees
        #     elif 'meter' in units and not 'k' in units:
        #         buffer_distance = 10_000 # Buffer by 10 km
        #     else:
        #         raise NotImplementedError(f'Unsupported units: {units}')
        # except Exception as e:
        #     logging.error(f'Error buffering dem: \n{e}')
        buffer_distance = self.BUFFER_DISTANCE
        minx, miny, maxx,maxy = self.get_ds_extent(ds)
        ds = None
        minx -= buffer_distance
        miny -= buffer_distance
        maxx +=  buffer_distance
        maxy +=  buffer_distance

        if buffer_distance == 0.1:
            minx = max(minx, -180)
            miny = max(miny, -90)
            maxx = min(maxx, 180)
            maxy = min(maxy, 90)
        # TODO figure out if gdal has errors for out of bounds projected units
        buffered_dem = os.path.join(self.DATA_DIR, 'dems', 'buffered', os.path.splitext(os.path.basename(dem))[0] + '_buff.tif')
        if os.path.exists(buffered_dem) and not self.OVERWRITE and self.hash_match(buffered_dem, dem):
            return buffered_dem

        dems = self.find_dems_in_extent(extent=(minx, miny, maxx, maxy))
        if not dems:
            logging.warning(f'Somehow no dems found in this extent: {(minx, miny, maxx, maxy)}')
            return
        
        vrt_options = gdal.BuildVRTOptions(resampleAlg='lanczos',
                                            outputSRS=projection,
                                            srcNodata=no_data_value,
                                            outputBounds=(minx, miny, maxx, maxy))
        gdal.BuildVRT(buffered_dem, dems, options=vrt_options)
        self.update_hash(buffered_dem, dem)
        return buffered_dem
        
    def _sizeof_fmt(self, num:int) -> str:
        """
        Take in an int number of bytes, outputs a string that is human readable
        """
        for unit in ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB"):
            if abs(num) < 1024.0:
                return f"{num:3.1f} {unit}"
            num /= 1024.0
        return f"{num:.1f} YB"

    def open_w_gdal(self, path: str) -> gdal.Dataset:
        ds = gdal.OpenEx(path)
        if not ds:
            logging.warning(f'Could not open {path}. Is this a valid file?')
        return ds
    
    def get_ds_extent(self, ds: gdal.Dataset) -> Tuple[float, ...]:
        geo = ds.GetGeoTransform()
        minx = geo[0]
        maxy = geo[3]
        maxx = minx + geo[1] * ds.RasterXSize
        miny = maxy + geo[5] * ds.RasterYSize
        return minx, miny, maxx, maxy
    
    def gpd_read(self, path: str,bbox: box = None,columns = None,) -> gpd.GeoDataFrame:
        """
        Reads .parquet, .shp, .gpkg, and potentially others and returns in a way we expect.
        """
        global GPD_ENGINE

        # Handle case where columns contain None or are empty
        if columns and (None in columns or not columns):
            columns = None

        if columns and 'geometry' not in columns:
            columns.append('geometry')

        # Read different file types
        if path.endswith((".parquet", ".geoparquet")):
            df = gpd.read_parquet(path, columns=columns)
            if bbox:
                df = df[df.geometry.intersects(bbox)]
        elif path.endswith((".shp", ".gpkg")):
            df = gpd.read_file(path, bbox=bbox, engine=GPD_ENGINE)
            if columns:
                df = df[columns]
        else:
            raise NotImplementedError(f"Unsupported file type: {path}")

        return df
         
    def get_geotransform_from_gdf(self, gdf: gpd.GeoDataFrame, x_res: float, y_res: float) -> Tuple[float, ...]:
         # Get the bounds of the GeoDataFrame
        bounds = gdf.total_bounds  # returns (minx, miny, maxx, maxy)

        # Calculate geotransform coefficients
        minx, miny, maxx, maxy = bounds
        x_min = minx
        y_max = maxy
        pixel_width = x_res
        pixel_height = -y_res  # y_res is negative because the origin is at the top-left corner

        return (x_min, pixel_width, 0, y_max, 0, pixel_height)
          
    def get_projection_from_gdf(self, gdf: gpd.GeoDataFrame) -> str:
        crs = gdf.crs
        if crs is None:
            raise ValueError("No CRS found in the GeoDataFrame")
        return crs.to_wkt()      
      
    def create_strm_file(self, strms_dems: tuple) -> list:
        global GEOMETRY_SAVE_EXTENSION
        strms, dems = strms_dems
        output = []
        # Get the total extent of all the DEMS
        big_minx, big_miny, big_maxx, big_maxy = None, None, None, None
        for dem in dems:
            ds = self.open_w_gdal(dem)
            if not ds: continue
            minx, miny, maxx, maxy = self.get_ds_extent(ds)
            big_minx = min(minx, big_minx) if big_minx else minx
            big_miny = min(miny, big_miny) if big_miny else miny
            big_maxx = max(maxx, big_maxx) if big_maxx else maxx
            big_maxy = max(maxy, big_maxy) if big_maxy else maxy
            ds_epsg = self.get_epsg(ds)
            ds = None
            
            strm_raster = os.path.join(self.DATA_DIR, 'stream_files', f"{os.path.splitext(os.path.basename(dem))[0]}__strm.tif")
            if not self.OVERWRITE and os.path.exists(strm_raster) and self.hash_match(strm_raster, dem):
                output.append(strm_raster)
        
        if len(output) == len(dems):
            return output
        

        if self.STREAM_ID == None:
            logging.warning(f"No stream id provided. We will try to assume that the first column is the id. If this is not the case, please provide the stream id.")
        
        tmp_streams = os.path.join(self.DATA_DIR, 'tmp', f"temp_streams_{hash(strms)}.{GEOMETRY_SAVE_EXTENSION}")
        # if len(strms) > 1:
        try:
            dfs = []
            for f in strms:
                with fiona.open(f, 'r') as src:
                    crs = src.crs
                if ds_epsg != crs.to_epsg():
                    transformer = Transformer.from_crs(f"EPSG:{ds_epsg}", crs.to_string(), always_xy=True) 
                    minx2, miny2 = transformer.transform(big_minx, big_miny)
                    maxx2, maxy2 =  transformer.transform(big_maxx, big_maxy)
                    bbox = box(minx2, miny2, maxx2, maxy2)
                else:
                    bbox = box(big_minx, big_miny, big_maxx, big_maxy)
                try:
                    df = self.gpd_read(f, columns=[self.STREAM_ID, 'geometry'], bbox=bbox)
                except NotImplementedError:
                    logging.warning(f"Skipping unsupported file: {f}")
                    continue
                except ValueError as e:
                    logging.error(f"Cannot find {self.STREAM_ID} in {f}")
                    raise e
                
                if not df.empty:
                    dfs.append(df.to_crs(ds_epsg))
            df = gpd.GeoDataFrame(pd.concat(dfs, ignore_index=True))
            
            if self.STREAM_ID is None or self.STREAM_ID == []:
                logging.info(f"Setting stream id as {df.columns[0]}")
                self.STREAM_ID = df.columns[0]
            
            if GEOMETRY_SAVE_EXTENSION == "parquet":
                df.to_parquet(tmp_streams)
            else:
                df.to_file(tmp_streams)
        except KeyError as e:
            logging.error(f"{self.STREAM_ID} not found in the stream files here: {self.STREAM_NETWORK_FOLDER}")
            return
        # else:
        #     with fiona.open(strms[0], 'r') as src:
        #         crs = src.crs
        #     if ds_epsg != crs.to_epsg():
        #         df = self.gpd_read(strms[0], columns=[self.STREAM_ID, 'geometry'], bbox=bbox)
        #         projection = self.get_projection_from_gdf(df)
        #         options = gdal.RasterizeOptions(attribute=self.STREAM_ID, outputType=gdal.GDT_UInt32,
        #                                         # Assume no negative IDs. DO NOT SET TO UINT64 
        #                                         format='GTiff', outputSRS=projection, 
        #                                         creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2"], 
        #                                         outputBounds=(minx, miny, maxx, maxy), noData=0, 
        #                                         width=width, height=height,
        #         )
        #     else:
        #         tmp_streams = strms[0]

        for dem in dems:
            strm_raster = os.path.join(self.DATA_DIR, 'stream_files', f"{os.path.basename(dem).split('.')[0].replace('_buff','')}__strm.tif")
            if strm_raster in output: continue
            
            ds = self.open_w_gdal(dem)
            minx, miny, maxx, maxy = self.get_ds_extent(ds)
            width=ds.RasterXSize
            height=ds.RasterYSize
            projection = ds.GetProjection()
            ds = None
            options = gdal.RasterizeOptions(attribute=self.STREAM_ID,
                                outputType=gdal.GDT_UInt32, # Assume no negative IDs. DO NOT SET TO UINT64
                                format='GTiff',
                                outputSRS=projection,
                                creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2"],
                                outputBounds=(minx, miny, maxx, maxy),
                                noData=0,
                                width=width,
                                height=height,
            )
            gdal.Rasterize(strm_raster, tmp_streams, options=options)
            self.update_hash(strm_raster, dem)
            output.append(strm_raster)
        if len(strms) > 1:
            os.remove(tmp_streams)
        return output

    def create_row_col_id_file(self, strm: str) -> str:
        if not strm:
            return
        row_col_file = os.path.join(self.DATA_DIR, 'rapid_files', f"{os.path.basename(strm).split('strm.')[0]}row_col_id.txt")
        if not self.OVERWRITE and os.path.exists(row_col_file) and self.hash_match(row_col_file, self.SIMULATION_FLOWFILE, strm):
            return row_col_file

        if self.SIMULATION_FLOWFILE.lower().endswith(('.csv', '.txt', '.nc','.nc3','.nc4')):
            if self.SIMULATION_FLOWFILE.lower().endswith(('.csv', '.txt')):
                df = pd.read_csv(self.SIMULATION_FLOWFILE, sep=',')
            elif self.SIMULATION_FLOWFILE.lower().endswith(('.nc','.nc3','.nc4')):
                try:
                    import xarray as xr
                except ImportError:
                    logging.error("Please install xarray to read netcdf files")
                    return
                try:
                    df = (xr.open_dataset(self.SIMULATION_FLOWFILE)
                        .to_dataframe()
                        .reset_index() # This gets the first column back instead of being an index
                    )
                except OSError:
                    logging.error("Please install netcdf4 to read netcdf files")
                    return
                    
            if not self.SIMULATION_ID_COLUMN:
                self.SIMULATION_ID_COLUMN =df.columns[0]
                self.multiprocess_data['SIMULATION_ID_COLUMN'] = df.columns[0]
            if self.SIMULATION_ID_COLUMN not in df:
                logging.warning(f"The id field you've entered is not in the file\nids entered: {self.SIMULATION_ID_COLUMN}, columns found: {list(df)}\n\tWe will assume the first column is the id column: {df.columns[0]}")
                self.SIMULATION_ID_COLUMN = df.columns[0]
                self.multiprocess_data['SIMULATION_ID_COLUMN'] = df.columns[0]
            if not self.SIMULATION_FLOW_COLUMN:
                cols = df.columns
            else:
                if isinstance(self.SIMULATION_FLOW_COLUMN, str):
                    if self.SIMULATION_FLOW_COLUMN not in df.columns:
                        raise ValueError(f"The flow field you've entered is not in the file\nflows entered: {self.SIMULATION_FLOW_COLUMN}, columns found: {list(df)}")
                    cols = [self.SIMULATION_ID_COLUMN, self.SIMULATION_FLOW_COLUMN]
                elif isinstance(self.SIMULATION_FLOW_COLUMN, list):
                    if not all(f in df.columns for f in self.SIMULATION_FLOW_COLUMN):
                        raise ValueError(f"The flow fields you've entered is not in the file\nflows entered: {self.SIMULATION_FLOW_COLUMN}, columns found: {list(df)}")
                    cols = [self.SIMULATION_ID_COLUMN] + self.SIMULATION_FLOW_COLUMN
                if self.BASE_FLOW_COLUMN:
                    if self.BASE_FLOW_COLUMN not in df.columns:
                        raise ValueError(f"The baseflow field you've entered is not in the file\nflows entered: {self.SIMULATION_FLOW_COLUMN}, columns found: {list(df)}")
                    cols.append(self.BASE_FLOW_COLUMN)
                    
            df = (
                df.drop_duplicates(self.SIMULATION_ID_COLUMN if self.SIMULATION_ID_COLUMN else None) 
                .dropna()
            )
            df = df[cols]
            if df.empty:
                logging.error(f"No data in the flow file: {self.SIMULATION_FLOWFILE}")
                return
        else:
            raise NotImplementedError(f"Unsupported file type: {self.SIMULATION_FLOWFILE}")
                
        # Now look through the Raster to find the appropriate Information and print to the FlowFile
        ds = gdal.Open(strm)
        data_array = ds.ReadAsArray()
        ds = None

        indices = np.where(data_array > 0)
        values = data_array[indices]
        matches = df[df[self.SIMULATION_ID_COLUMN].isin(values)].shape[0]

        if matches == 0:
            logging.warning(f"{matches} id(s) out of {df.shape[0]} from your input file are present in the stream raster...")
        
        (
            pd.DataFrame({'ROW': indices[0], 'COL': indices[1], self.SIMULATION_ID_COLUMN: values})
            .merge(df, on=self.SIMULATION_ID_COLUMN, how='left')
            .fillna(0)
            .to_csv(row_col_file, sep="\t", index=False)
        )
        self.update_hash(row_col_file, self.SIMULATION_FLOWFILE, strm)
        return row_col_file

    def create_flood_flowfile(self, strm: str) -> str:
        if not strm:
            return
        flowfile = os.path.join(self.DATA_DIR, 'flow_files', f"{os.path.basename(strm).split('strm.')[0]}flow.txt")
        if not self.OVERWRITE and os.path.exists(flowfile) and self.hash_match(flowfile, self.FLOOD_FLOWFILE, strm):
            return flowfile

        if self.FLOOD_FLOWFILE.endswith(('.csv', '.txt')):
            df = (
                pd.read_csv(self.FLOOD_FLOWFILE, sep=',')
            )
            id_col = df.columns[0]
            df = (
                df.drop_duplicates(id_col) 
                .dropna()
            )
            if df.empty:
                logging.error(f"No data in the flow file: {self.FLOOD_FLOWFILE}")
                return
        else:
            raise NotImplementedError(f"Unsupported file type: {self.FLOOD_FLOWFILE}")
                
        # Now look through the Raster to find the appropriate Information and print to the FlowFile
        ds = gdal.Open(strm)
        data_array = ds.ReadAsArray()
        ds = None

        ids = np.unique(data_array)[1:]
        
        (
            df[df[id_col].isin(ids)]
            .to_csv(flowfile,index=False)
        )
        self.update_hash(flowfile, self.FLOOD_FLOWFILE, strm)
        return flowfile

    def create_land_use(self, dem: str) -> str:
        global GEOMETRY_SAVE_EXTENSION
        lu_file = os.path.join(self.DATA_DIR, 'land_use', f"{os.path.basename(dem).split('.')[0]}__lu.vrt")
        if not self.OVERWRITE and os.path.exists(lu_file) and self.hash_match(dem, lu_file):
            return lu_file
        dem_ds = self.open_w_gdal(dem)
        if not dem_ds: 
            logging.warning(f'Could not open {dem}. Skipping...')
            return
        
        dem_epsg = self.get_epsg(dem_ds)
        projection = dem_ds.GetProjection()
        dem_spatial_ref = dem_ds.GetSpatialRef()

        minx, miny, maxx, maxy = self.get_ds_extent(dem_ds)
        x_res = abs(dem_ds.GetGeoTransform()[1])
        y_res = abs(dem_ds.GetGeoTransform()[5])
        dem_ds = None

        if os.path.isdir(self.LAND_USE_FOLDER):
            filenames = [os.path.join(self.LAND_USE_FOLDER,f) for f in os.listdir(self.LAND_USE_FOLDER) if f.endswith(('.tif'))]
        else:
            filenames = [self.LAND_USE_FOLDER]

        if not self.check_has_same_projection(filenames):
            logging.error("Some land use files have different projections. Exiting...")
            raise NotImplementedError("Different projections")
        if not filenames:
            logging.warning(f"No land use files found in {self.LAND_USE_FOLDER}")
            return
        
        # Loop over every file in the land use folder and check if it intersects with the dem
        lu_epsg = self.get_epsg(self.open_w_gdal(filenames[0]))
        if dem_epsg != lu_epsg:
            out_spatial_ref = osr.SpatialReference()
            out_spatial_ref.ImportFromEPSG(lu_epsg)
            coordTrans = osr.CoordinateTransformation(dem_spatial_ref, out_spatial_ref)
        files_to_use = []
        for f in filenames:
            ds = self.open_w_gdal(f)
            if not ds: 
                logging.warning(f'Could not open {f}. Skipping...')
                continue
            minx2, miny2, maxx2, maxy2 = self.get_ds_extent(ds)
            if dem_epsg != lu_epsg:
                minx2, miny2, _ = coordTrans.TransformPoint(minx, miny)
                maxx2, maxy2, _ = coordTrans.TransformPoint(maxx, maxy)
                
            if self.is_in_extent(ds, (minx2, miny2, maxx2, maxy2)):
                if ds.ReadAsArray().max() > 100:
                    msg = f"Land use file {f} has values over 100. AutoRoute cannot read this. Please reclassify."
                    logging.error(msg)
                    raise ValueError(msg)
                files_to_use.append(f)
            ds = None
        
        if not files_to_use:
            logging.warning(f"No land use files found in the extent of {dem}")
            return
        
        if dem_epsg != lu_epsg:
            options = gdal.WarpOptions(
                format='GTiff',
                dstSRS=projection,
                outputBounds=(minx, miny, maxx, maxy),
                outputType=gdal.GDT_Byte,  
                multithread=True, 
                xRes=x_res,
                yRes=y_res,
                creationOptions = ["COMPRESS=DEFLATE", "PREDICTOR=2", "NUM_THREADS=ALL_CPUS"]
            )    
            lu_file = lu_file.replace('.vrt', '.tif')
            gdal.Warp(lu_file, files_to_use, options=options) 
        else:
            vrt_options = gdal.BuildVRTOptions(outputSRS=projection,
                                           outputBounds=(minx, miny, maxx, maxy),
                                           resampleAlg='nearest',
                                           xRes=x_res,
                                           yRes=y_res,
                                           )
            gdal.BuildVRT(lu_file, files_to_use, options=vrt_options)
        self.hash_match(dem, lu_file)
        return lu_file

    def check_has_same_projection(self, files: List[str]) -> bool:
        if not files: return True
        try:
            ds_code = self.get_epsg(self.open_w_gdal(files[0]))
            for f in files[1:]:
                if ds_code != self.get_epsg(self.open_w_gdal(f)):
                    return False
        except:
            logging.warning(f"Problems with a file")
            return False
        return True

    def get_epsg(self, ds: gdal.Dataset) -> int:
        dem_spatial_ref = ds.GetSpatialRef()
        dem_sr = osr.SpatialReference(str(dem_spatial_ref)) # load projection
        return int(dem_sr.GetAuthorityCode(None)) # get EPSG code

    def list_to_sublists(self, alist: List[Any], n: int) -> List[List[Any]]:
        return [alist[x:x+n] for x in range(0, len(alist), n)]
    
    def crop(self, dem: str) -> str:
        ds = self.open_w_gdal(dem)
        if not ds:
            logging.warning(f'Could not open {dem}. Skipping...')
            return
        projection = ds.GetProjection()
        no_data_value = ds.GetRasterBand(1).GetNoDataValue()
        minx, miny, maxx, maxy = self.get_ds_extent(ds)
        ds = None

        # Crop the DEM to the min of the extent and the DEM itself
        minx = max(minx, self.EXTENT[0])
        miny = max(miny, self.EXTENT[1])
        maxx = min(maxx, self.EXTENT[2])
        maxy = min(maxy, self.EXTENT[3])

        cropped_dem = os.path.join(self.DATA_DIR, 'dems', 'cropped', os.path.splitext(os.path.basename(dem))[0] + '_crop.tif')
        if not self.OVERWRITE and os.path.exists(cropped_dem) and self.hash_match(dem, cropped_dem):
            #logging.info(f"{cropped_dem} already exists. Skipping...")
            return

        vrt_options = gdal.BuildVRTOptions(resampleAlg='nearest',
                                        outputSRS=projection,
                                        srcNodata=no_data_value,
                                        outputBounds=(minx, miny, maxx, maxy))
        gdal.BuildVRT(cropped_dem, dem, options=vrt_options)
        self.update_hash(dem, cropped_dem)
        return cropped_dem
            
    def create_mifn_file(self, dem: str, strm: str, lu: str, rapid_flow_file: str, flowfile: str) -> str:
        """
        We always make a new main input file for every run. They are cheap to make and we don't want to accidentally keep one 
        that needs to be overwritten.
        """
        # Format path strings
        dem = self._format_path(dem)
        if not os.path.isabs(dem):
            dem = os.path.abspath(dem)
        mifn = os.path.join(self.DATA_DIR, 'mifns', f"{os.path.basename(dem).split('.')[0]}__mifn.txt")
        
        meta_file = os.path.join(self.DATA_DIR, 'meta_files', f"{os.path.basename(dem).split('.')[0]}__meta.txt")

        output = []
        self._warn_DNE('DEM', dem)
        self._check_type('DEM', dem, ['.tif','.vrt'])
        self._write(output,'DEM_File',dem)

        output.append('\n')
        self._write(output,'# AutoRoute Inputs')
        output.append('\n')

        if strm:
            strm = self._format_path(strm)
            self._warn_DNE('Stream File', strm)
            self._check_type('Stream Fil', strm, ['.tif', '.vrt'])
            self._write(output,'Stream_File',strm)

        # Open DEM to see if projected or geographic units
        ds = gdal.Open(dem)
        srs = osr.SpatialReference(wkt=ds.GetProjection())
        nrows = ds.RasterYSize
        ds = None
        if srs.IsProjected():
            units = srs.GetLinearUnitsName().lower()
            if "meter" in units:
                if "k" in units:
                    self._write(output,'Spatial_Units',"km")
                else:
                    self._write(output,'Spatial_Units',"m")
            else:
                raise ValueError(f"Unsupported units: {units}")
        else:
            units = srs.GetAngularUnitsName().lower()
            if "degree" in units:
                self._write(output,'Spatial_Units',"deg")
            else:
                raise ValueError(f"Unsupported units: {units}")

        if rapid_flow_file:
            rapid_flow_file = self._format_path(rapid_flow_file)
            self._warn_DNE('Flow File', rapid_flow_file)
            self._check_type('Flow File', rapid_flow_file, ['.txt'])
            if self.USE_PYTHON:
                self._write(output,'Flow_File',rapid_flow_file)
            else:
                self._write(output,'Flow_RAPIDFile',rapid_flow_file)

            self._write(output,'RowCol_From_RAPIDFile')
            if not self.SIMULATION_ID_COLUMN:
                logging.warning('Flow ID is not specified!!')
            else:
                if self.USE_PYTHON:
                    self._write(output,'Flow_File_ID',self.SIMULATION_ID_COLUMN)
                else:
                    self._write(output,'RAPID_Flow_ID', self.SIMULATION_ID_COLUMN)
            if not self.SIMULATION_FLOW_COLUMN:
                logging.warning('Flow Params are not specified!!')
            else:
                if not isinstance(self.SIMULATION_FLOW_COLUMN, list):
                    self.SIMULATION_FLOW_COLUMN = [self.SIMULATION_FLOW_COLUMN]
                self._write(output,'RAPID_Flow_Param', " ".join(self.SIMULATION_FLOW_COLUMN))
            if self.RAPID_Subtract_BaseFlow:
                if not self.BASE_FLOW_COLUMN:
                    logging.warning('Base Flow Parameter is not specified, not subtracting baseflow')
                else:
                    if self.USE_PYTHON:
                        self._write(output,'Flow_File_BF',self.BASE_FLOW_COLUMN)
                    else:
                        self._write(output,'RAPID_BaseFlow_Param',self.BASE_FLOW_COLUMN)
                    self._write(output,'RAPID_Subtract_BaseFlow')

        if self.VDT:
            vdt = os.path.join(self._format_path(self.VDT), f"{os.path.basename(dem).split('.')[0]}__vdt.txt")
            if not os.path.isabs(vdt):
                vdt = os.path.abspath(vdt)
        else:
            vdt = os.path.join(self.DATA_DIR, 'vdts', f"{os.path.basename(dem).split('.')[0]}__vdt.txt")
        if self.USE_PYTHON:
            if self.VDT:
                curve_file = os.path.join(self._format_path(self.VDT), f"{os.path.basename(dem).split('.')[0]}__curve.txt")
                if not os.path.isabs(vdt):
                    vdt = os.path.abspath(vdt)
            else:
                curve_file = os.path.join(self.DATA_DIR, 'curves', f"{os.path.basename(dem).split('.')[0]}__curve.txt")
            self._write(output,'Print_Curve_File',curve_file)    
            
        self._write(output,'Print_VDT_Database',vdt)
        self._write(output,'Print_VDT_Database_NumIterations',self.num_iterations)

        meta_file = self._format_path(meta_file)
        self._write(output,'Meta_File',meta_file)

        if self.convert_cfs_to_cms: self._write(output,'CONVERT_Q_CFS_TO_CMS')

        self._write(output,'X_Section_Dist',self.x_distance)
        self._write(output,'Q_Limit',self.q_limit)
        self._write(output,'Gen_Dir_Dist',self.direction_distance)
        self._write(output,'Gen_Slope_Dist',self.slope_distance)
        if self.weight_angles:
            self._write(output,'Weight_Angles',self.weight_angles)
        if self.use_prev_d_4_xs == 0:
            self._write(output,'Use_Prev_D_4_XS',0)
        elif self.use_prev_d_4_xs != 1:
            logging.warning('Use_Prev_D_4_XS must be 0 or 1. Will use AutoRoute\'s default value of 1')
        self._write(output,'ADJUST_FLOW_BY_FRACTION',self.adjust_flow)
        if self.Str_Limit_Val: self._write(output,'Str_Limit_Val',self.Str_Limit_Val)
        if not math.isinf(self.UP_Str_Limit_Val) and self.UP_Str_Limit_Val > 0: self._write(output,'UP_Str_Limit_Val',self.UP_Str_Limit_Val)
        if self.row_start: self._write(output,'Layer_Row_Start',self.row_start)
        if self.row_end < nrows and self.row_end > 0: self._write(output,'Layer_Row_End',self.row_end)

        if self.degree_manip > 0 and self.degree_interval > 0:
            self._write(output,'Degree_Manip',self.degree_manip)
            self._write(output,'Degree_Interval',self.degree_interval)

        if lu:
            lu_raster = self._format_path(lu)
            self._warn_DNE('Land Use', lu_raster)
            self._check_type('Land Use', lu_raster, ['.tif', '.vrt'])
            self._write(output,'LU_Raster_SameRes',lu_raster)
            if not self.MANNINGS_TABLE:
                logging.warning('No mannings table for the Land Use raster!')
            else:
                if not os.path.isabs(self.MANNINGS_TABLE):
                    self.MANNINGS_TABLE = os.path.abspath(self.MANNINGS_TABLE)
                mannings_table = self._format_path(self.MANNINGS_TABLE)
                self._write(output,'LU_Manning_n',mannings_table)
        else:
            self._write(output,'Man_n',self.man_n)

        if self.low_spot_distance is not None:
            if self.low_spot_is_meters:
                self._write(output,'Low_Spot_Dist_m',self.low_spot_distance)
            else:
                self._write(output,'Low_Spot_Range',self.low_spot_distance)
            if self.low_spot_use_box:
                self._write(output,'Low_Spot_Range_Box')
                self._write(output,'Low_Spot_Range_Box_Size',self.box_size)
        
        if self.find_flat:
            self._write(output,'Low_Spot_Find_Flat')
            if self.low_spot_find_flat_cutoff < float('inf'):
                self._write(output,'Low_Spot_Range_FlowCutoff',self.low_spot_find_flat_cutoff)

        if self.run_bathymetry:
            self._write(output,'Bathymetry')
            if self.ar_bathy_file:
                if not os.path.isabs(self.ar_bathy_file):
                    self.ar_bathy_file = os.path.abspath(self.ar_bathy_file)
                os.makedirs(self.ar_bathy_file, exist_ok=True)
                bathy_file = os.path.join(self.ar_bathy_file, f"{os.path.basename(dem).split('.')[0]}__ar_bathy.tif")
            else:
                bathy_file = os.path.join(self.DATA_DIR, 'bathymetry', f"{os.path.basename(dem).split('.')[0]}__ar_bathy.tif")
            if self.AUTOROUTE_PYTHON_MAIN:
                self._write(output,'BATHY_Out_File',bathy_file)
            else:
                self._write(output,'BATHY_Out_File',bathy_file)
            self._write(output,'Bathymetry_Alpha',self.bathy_alpha)
            

            if self.bathy_method == 'Parabolic':
                self._write(output,'Bathymetry_Method',0)
            elif self.bathy_method == 'Left Bank Quadratic':
                self._write(output,'Bathymetry_Method', 1)
                self._write(output,'Bathymetry_XMaxDepth',self.bathy_x_max_depth)
                self._write(output,'Bathymetry_YShallow',self.bathy_y_shallow)
            elif self.bathy_method == 'Right Bank Quadratic':
                self._write(output,'Bathymetry_Method', 2)
                self._write(output,'Bathymetry_XMaxDepth',self.bathy_x_max_depth)
                self._write(output,'Bathymetry_YShallow',self.bathy_y_shallow)
            elif self.bathy_method == 'Double Quadratic':
                self._write(output,'Bathymetry_Method', 3)
                self._write(output,'Bathymetry_XMaxDepth',self.bathy_x_max_depth)
                self._write(output,'Bathymetry_YShallow',self.bathy_y_shallow)
            elif self.bathy_method == 'Trapezoidal':
                self._write(output,'Bathymetry_Method', 4)
                self._write(output,'Bathymetry_XMaxDepth',self.bathy_x_max_depth)
            else: self._write(output,'Bathymetry_Method', 5)

        if self.da_flow_param: self._write(output, 'RAPID_DA_or_Flow_Param',self.da_flow_param)

        output.append('\n')
        self._write(output,'# FloodSpreader Inputs')
        output.append('\n')

        if flowfile:
            id_flow_file = self._format_path(flowfile)
            self._warn_DNE('ID Flow File', id_flow_file)
            self._check_type('ID Flow File', id_flow_file, ['.txt','.csv'])
            self._write(output,'Comid_Flow_File',id_flow_file)

        if self.omit_outliers:
            if self.omit_outliers == 'Flood Bad Cells':
                self._write(output,'Flood_BadCells')
            elif self.omit_outliers == 'Use AutoRoute Depths':
                self._write(output,'FloodSpreader_Use_AR_Depths')
            elif self.omit_outliers == 'Smooth Water Surface Elevation':
                self._write(output,'FloodSpreader_SmoothWSE')
                self._write(output,'FloodSpreader_SmoothWSE_SearchDist',self.wse_search_dist)
                self._write(output,'FloodSpreader_SmoothWSE_FractStDev',self.wse_threshold)
                if self.wse_remove_three:
                    self._write(output,'FloodSpreader_SmoothWSE_RemoveHighThree')
            elif self.omit_outliers == 'Use AutoRoute Depths (StDev)':
                self._write(output,'FloodSpreader_Use_AR_Depths_StDev')
            elif self.omit_outliers == 'Specify Depth' and self.specify_depth:
                self._write(output,'FloodSpreader_SpecifyDepth',self.specify_depth)
            else:
                logging.warning(f'Unknown outlier omission option: {self.omit_outliers}')

        if self.twd_factor != 1.5:
            self._write(output,'TopWidthDistanceFactor',self.twd_factor)
        if self.only_streams: self._write(output,'FloodSpreader_JustStrmDepths')
        if self.use_ar_top_widths: self._write(output,'FloodSpreader_Use_AR_TopWidth')
        if self.flood_local: self._write(output,'FloodLocalOnly')

        if self.DEPTH_MAP:
            self.DEPTH_MAP = self._format_path(self.DEPTH_MAP)
            os.makedirs(self.DEPTH_MAP, exist_ok=True)
            if not os.path.isabs(self.DEPTH_MAP):
                self.DEPTH_MAP = os.path.abspath(self.DEPTH_MAP)
            depth_map = self._format_path(os.path.join(self.DEPTH_MAP, f"{os.path.basename(dem).split('.')[0]}__depth.tif"))
            if self.OVERWRITE or not os.path.exists(depth_map):
                self._check_type('Depth Map',depth_map,['.tif'])
                self._write(output,'OutDEP',depth_map)

        if self.FLOOD_MAP:
            self.FLOOD_MAP = self._format_path(self.FLOOD_MAP)
            os.makedirs(self.FLOOD_MAP, exist_ok=True)
            if not os.path.isabs(self.FLOOD_MAP):
                self.FLOOD_MAP = os.path.abspath(self.FLOOD_MAP)
            flood_map = self._format_path(os.path.join(self.FLOOD_MAP, f"{os.path.basename(dem).split('.')[0]}__flood.tif"))
            if self.OVERWRITE or not os.path.exists(flood_map):
                self._check_type('Flood Map',flood_map,['.tif'])
                self._write(output,'OutFLD',flood_map)

        if self.VELOCITY_MAP:
            self.VELOCITY_MAP = self._format_path(self.VELOCITY_MAP)
            os.makedirs(self.VELOCITY_MAP, exist_ok=True)
            if not os.path.isabs(self.VELOCITY_MAP):
                self.VELOCITY_MAP = os.path.abspath(self.VELOCITY_MAP)
            velocity_map = self._format_path(os.path.join(self.VELOCITY_MAP, f"{os.path.basename(dem).split('.')[0]}__vel.tif"))
            if self.OVERWRITE or not os.path.exists(velocity_map):
                self._check_type('Velocity Map',velocity_map,['.tif'])
                self._write(output,'OutVEL',velocity_map)

        if self.WSE_MAP:
            self.WSE_MAP = self._format_path(self.WSE_MAP)
            os.makedirs(self.WSE_MAP, exist_ok=True)
            if not os.path.isabs(self.WSE_MAP):
                self.WSE_MAP = os.path.abspath(self.WSE_MAP)
            wse_map = self._format_path(os.path.join(self.WSE_MAP, f"{os.path.basename(dem).split('.')[0]}__wse.tif"))
            if self.OVERWRITE or not os.path.exists(wse_map):
                self._check_type('WSE Map',wse_map,['.tif'])
                self._write(output,'OutWSE',wse_map)

        if self.run_bathymetry and self.fs_bathy_file: 
            os.makedirs(self.fs_bathy_file, exist_ok=True)
            if not os.path.isabs(self.fs_bathy_file):
                self.fs_bathy_file = os.path.abspath(self.fs_bathy_file)
            fs_bathy_file = os.path.join(self._format_path(self.fs_bathy_file), f"{os.path.basename(dem).split('.')[0]}__fs_bathy.tif")
            self._write(output,'FSOutBATHY', fs_bathy_file)
            if self.fs_bathy_smooth_method == 'Linear Interpolation':
                self._write(output,'Bathy_LinearInterpolation')
            elif self.fs_bathy_smooth_method == 'Inverse-Distance Weighted':
                self._write(output,'BathyTopWidthDistanceFactor', self.bathy_twd_factor)

        contents = "\n".join(output)
        with open(mifn, 'w', encoding='utf-8') as f:
            f.write(contents)
            
        return mifn

    def _format_path(self,file_path: str) -> str:
        """
        Function added so that windows paths that pad with quotation marks can be valid
        """
        if any(file_path.endswith(s) and file_path.startswith(s) for s in {'"',"'"}):
            return file_path[1:-1]
        return file_path
        
    def _write(self, f: Union[List, TextIO], Card, Argument = '') -> None:
        if not isinstance(f, list):
            if Argument:
                f.write(f"{Card}\t{Argument}\n")
            else:
                f.write(f"{Card}\n")
        else:
            if Argument:
                f.append(f"{Card}\t{Argument}")
            else:
                f.append(f"{Card}")
            
    def _warn_DNE(self, card: str, value: str) -> None:
        if value:
            if not os.path.exists(value):
                logging.warning(f"The file {value} for {card} does not exist: ")

    def _check_type(self, card: str, value: str, types: List[str]) -> None:
        if not any(value.endswith(t) for t in types):
            logging.warning(f"{card} is {os.path.basename(value)}, which does not have a valid file type ({','.join(types)})")
    
    def _zip_files(self, *args) -> Set[Tuple[str]]:
        """"
        Given a bunch of lists, sort each list so that the filenames match, and create a list of tuples 
        """
        # Get max length of all args
        dems = {os.path.splitext(os.path.basename(f))[0]: f for f in args[0] if f.endswith('.tif') or f.endswith('.vrt')}
        strms = {os.path.splitext(os.path.basename(f))[0].split('__')[0]: f for f in args[1] if f is not None}
        lus = {os.path.splitext(os.path.basename(f))[0].split('__')[0]: f for f in args[2] if f is not None}
        row_col_ids = {os.path.splitext(os.path.basename(f))[0].split('__')[0]: f for f in args[3] if f is not None}
        flow_ids = {os.path.splitext(os.path.basename(f))[0].split('__')[0]: f for f in args[4] if f is not None}
        
        output = set({})
        for key, value in  dems.items():
            out_tuple = [value]
            out_tuple.append(strms.get(key, ''))
            out_tuple.append(lus.get(key, ''))
            out_tuple.append(row_col_ids.get(key, ''))
            out_tuple.append(flow_ids.get(key, ''))
            
            output.add(tuple(out_tuple))

        return output

    def run_autoroute(self, mifn: str) -> None:
        try:
            if self.USE_PYTHON:
                try:
                    self.AUTOROUTE_PYTHON_MAIN(mifn)
                except Exception as e:
                    msg = f'Error running AutoRoutePy'
                    logging.error(msg)
                    raise e
                return
            
            exe = self._format_path(self.AUTOROUTE.strip())
            if not os.path.exists(exe):
                logging.error(f"AutoRoute executable not found: {exe}")
                return
            mifn = self._format_path(mifn.strip())

            vdt = self.get_item_from_mifn(mifn, key='Print_VDT_Database')
            contents = open(vdt).read()
            if not self.OVERWRITE and vdt and os.path.exists(vdt) and self.hash_match(vdt, contents):
                return

            process = subprocess.run(f'conda activate {self.AUTOROUTE_CONDA_ENV} && echo "a" | {exe} {mifn}', # We echo a dummy input in so that AutoRoute can terminate if some input is wrong
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     shell=True,)
            line = ''
            for l in process.stdout.decode('utf-8').splitlines():
                if 'error' in l.lower() and not any({'Perimeter' in l, 'Area' in l,  'Finder' in l}) or 'PROBLEMS' in l:
                    line = l + process.stderr.decode('utf-8')
                    break
                
            if line:
                logging.error(f"Error running AutoRoute: {line}")
            elif process.returncode != 0:
                logging.error(f"Error running AutoRoute: {process.stderr.decode('utf-8')}")

            self.update_hash(vdt, contents)
            return process.stdout.decode('utf-8') + process.stderr.decode('utf-8')
        except Exception as e:
            logging.error(f"Error running autoroute: {e}")

    def run_floodspreader(self, mifn: str) -> None:
        try:
            exe = self._format_path(self.FLOODSPREADER.strip())
            if not os.path.exists(exe):
                logging.error(f"FloodSpreader executable not found: {exe}")
                return
            
            mifn = self._format_path(mifn.strip())

            fld_map = self.get_item_from_mifn(mifn, key='OutFLD')
            dep_map = self.get_item_from_mifn(mifn, key='OutDEP')
            vel_map = self.get_item_from_mifn(mifn, key='OutVEL')
            wse_map = self.get_item_from_mifn(mifn, key='OutWSE')
            fs_bathy_file = self.get_item_from_mifn(mifn, key='FSOutBATHY')
            maps = {fld_map, dep_map, vel_map, wse_map, fs_bathy_file} - {""}
            
            if not self.OVERWRITE and all(os.path.exists(m) for m in maps) and self.hash_match(mifn, *maps):
                return
            # We must remove these maps in order for FloodSpreader to succesfuly run
            {os.remove(m) for m in maps if os.path.exists(m)}
            
            process = subprocess.run(f'conda activate {self.AUTOROUTE_CONDA_ENV} && echo "a" | {exe} {mifn}', # We echo a dummy input in so that AutoRoute can terminate if some input is wrong
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE,
                                     shell=True,)
        except Exception as e:
            logging.error(f"Error running floodspreader: {e}")
            return e

        line = ''
        for l in process.stdout.decode('utf-8').splitlines():
            if 'error' in l.lower() and not any({'Perimeter' in l, 'Area' in l,  'Finder' in l}) or 'PROBLEMS' in l:
                line = l + process.stderr.decode('utf-8')
                break
            
        if line:
            logging.error(f"Error running FloodSpreader: {line}")
        elif process.returncode != 0:
            logging.error(f"Error running FloodSpreader: {process.stderr.decode('utf-8')}")
            
        self.update_hash(mifn, *maps)
        return process.stdout.decode('utf-8') + process.stderr.decode('utf-8')
    
    def get_item_from_mifn(self, mifn: str, key: str) -> str:
        """
        Given a main input file, extract the vdt file from it
        """
        try:
            df = pd.read_csv(mifn, sep="\t", header=None).T 
            df = df.rename(columns=df.iloc[0]).iloc[1:]
            if key not in df.columns:
                return ""
            return df[key].values[0]
        except Exception as e:
            logging.error(f"Error reading '{key}' from {mifn}: {e}")
            return ""

    def test_ok(self):
        if self.AUTOROUTE or self.FLOODSPREADER:
            process = subprocess.run(f'conda activate {self.AUTOROUTE_CONDA_ENV}',
                                        stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.PIPE,
                                        shell=True,)
            if process.returncode != 0:
                logging.error(f"Error activating conda environment: {process.stderr.decode('utf-8')}")
                raise ValueError(f"Error activating conda environment: {process.stderr.decode('utf-8')}")
            
    def optimize_outputs(self, mifn: str):
        try:
            fld_map = self.get_item_from_mifn(mifn, key='OutFLD')
            dep_map = self.get_item_from_mifn(mifn, key='OutDEP')
            vel_map = self.get_item_from_mifn(mifn, key='OutVEL')
            wse_map = self.get_item_from_mifn(mifn, key='OutWSE')
            fs_bathy = self.get_item_from_mifn(mifn, key='FSOutBATHY')
            ar_bathy = self.get_item_from_mifn(mifn, key='BATHY_Out_File')
            maps = {fld_map, dep_map, vel_map, wse_map, fs_bathy, ar_bathy} - {""}
            for m in maps:
                if not os.path.exists(m) or self.hash_match(m):
                    continue # Skip unmodified files and files that don't exist
                
                ds = gdal.Open(m)
                geotransform = ds.GetGeoTransform()
                projection = ds.GetProjection()
                array = ds.ReadAsArray()
                ds = None

                noData = 0
                if 'flood' in os.path.basename(m):
                    type = gdal.GDT_Byte
                    predictor = 2       
                else:
                    type = gdal.GDT_Float32
                    predictor = 3
                    if 'wse' in os.path.basename(m) or 'bathy' in os.path.basename(m):
                        noData = -9999

                # Delete the file and resave
                os.remove(m)
                driver: gdal.Driver = gdal.GetDriverByName('GTiff')
                ds: gdal.Dataset = driver.Create(m, array.shape[1], array.shape[0], 1, 
                                                    type, options=["COMPRESS=DEFLATE", f"PREDICTOR={predictor}"])
                ds.SetGeoTransform(geotransform)
                ds.SetProjection(projection)
                ds.GetRasterBand(1).WriteArray(array)
                ds.GetRasterBand(1).SetNoDataValue(noData)
                ds = None
                self.update_hash(m)
        except Exception as e:
            logging.error(f"Error cleaning outputs: {e}")

    def create_fname(self, minx: float, miny: float, maxx: float, 
                     maxy: float,_type: str = '.vrt',append: str = '') -> str:
        return f"{self.format_coord(miny, True)}{self.format_coord(minx, False)}__{self.format_coord(maxy, True)}{self.format_coord(maxx, False)}{append}{_type}"

    def format_coord(self, value: float, is_latitude: bool) -> str:
        """Helper function to format coordinates with direction and rounded value."""
        direction = 'S' if value < 0 else 'N' if is_latitude else 'W' if value < 0 else 'E'
        return f"{direction}{str(round(abs(value), 3)).replace('.', '_')}"
    
    def _hash(self, file_path: str, *args) -> str:
        string = f"{os.path.getmtime(file_path)}{os.path.getsize(file_path)}{os.path.basename(file_path)}"
        if args:
            for arg in args:
                if os.path.isfile(arg):
                    string += f"{os.path.getmtime(arg)}{os.path.getsize(arg)}{os.path.basename(arg)}"
                    continue
                string += str(arg)
                
        return hashlib.blake2b(string.encode()).hexdigest()
    
    def hash_match(self, file: str, *args) -> bool:
        return self._hash(file, *args) == self._file_hash_dict.get(self.create_key(file), '')
    
    def update_hash(self, file: str, *args) -> None:
        self._file_hash_dict[self.create_key(file)] = self._hash(file, *args)
        
    def create_key(self, *args):
        string = []
        for arg in args:
            if os.path.isfile(arg):
                string.append(f"{os.path.getmtime(arg)}{os.path.getsize(arg)}{os.path.basename(arg)}")
                continue
            string.append(str(arg))
        return hashlib.blake2b(''.join(string).encode()).hexdigest()
      
    def map_dems_and_streams(self, dems: List[str]):
        """
        The purpose of this function is to find what dems and streams intersect with each other.
        We want any dems that are wholly contained in one stream file to be associated with that file.
        Any dems that need two of the same stream files should be associated with that file.
        And so on...
        """
        if os.path.isdir(self.STREAM_NETWORK_FOLDER):
            streams = [os.path.join(self.STREAM_NETWORK_FOLDER,f) for f in os.listdir(self.STREAM_NETWORK_FOLDER) if f.endswith(('.shp', '.gpkg', '.parquet', '.geoparquet'))]
        else:
            streams = [self.STREAM_NETWORK_FOLDER]
        if not streams:
            msg = f"No stream files found in {self.STREAM_NETWORK_FOLDER}"
            logging.error(msg)
            raise FileNotFoundError(msg)
        
        streams_list = []
        for stream in streams:
            with fiona.open(stream, 'r') as src:
                crs = src.crs
                f_extent = src.bounds
                streams_list.append((stream, crs, f_extent))
                
        dems_list = []
        for dem in dems:
            ds = self.open_w_gdal(dem)  # Replace with your actual method to open with GDAL
            ds_epsg = self.get_epsg(ds)  # Replace with your actual method to get EPSG
            minx, miny, maxx, maxy = self.get_ds_extent(ds)  # Replace with your actual method to get extent
            dems_list.append((dem, ds_epsg, (minx, miny, maxx, maxy)))
            
        mapping_dict = {}
        for dem, ds_epsg, dem_extent in dems_list:
            mapping_dict[dem] = []
            for stream_name, crs, f_extent in streams_list:
                minx, miny, maxx, maxy = dem_extent
                if ds_epsg != crs.to_epsg():
                    transformer = Transformer.from_crs(f"EPSG:{ds_epsg}", crs.to_string(), always_xy=True)
                    minx, miny = transformer.transform(minx, miny)
                    maxx, maxy = transformer.transform(maxx, maxy)
                if self._isin(minx, miny, maxx, maxy, f_extent):
                    mapping_dict[dem].append(stream_name)
        
        invertion = {}
        for dem, streams in mapping_dict.items():
            if not streams:
                logging.warning(f"No stream files found that intersect {dem}")
                continue
            s = tuple(streams)
            if s not in invertion:
                invertion[s] = []
            invertion[s].append(dem)
        
        return list(invertion.items())
            
    def save_hashes(self) -> None:
        with open(self._hash_file, 'w') as f:
            json.dump(dict(self._file_hash_dict), f, indent=4)