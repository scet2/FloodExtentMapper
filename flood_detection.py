import rasterio
from rasterio import features
from rasterio.windows import Window, transform as window_transform
from rasterio.warp import reproject, Resampling
import numpy as np
from scipy.ndimage import uniform_filter
import geopandas as gpd
import pandas as pd
import shapely
import os
import argparse


def load_scene(filepath):
     with rasterio.open(filepath) as dataset:
          array = dataset.read(1)
          crs = dataset.crs
          transform = dataset.transform

     return array, crs, transform


def load_and_align(filepath, ref_transform, ref_shape, sar_crs):
     destination = np.empty(ref_shape, dtype=np.float32)

     array, crs, transform = load_scene(filepath)

     reproject(
          source=array,
          destination=destination,
          src_transform=transform,
          src_crs=crs,
          dst_transform=ref_transform,
          dst_crs=sar_crs,
          resampling=Resampling.bilinear
     )

     return destination


def to_decibels(array, epsilon=1e-10):

     return 10 * np.log10(array + epsilon)


def lee_filter(image, window_size=7, enl=4.4):
     local_mean = uniform_filter(image, size = window_size)

     mse = uniform_filter(image**2, size = window_size)

     local_var = mse - local_mean**2

     noise_var = 1 / enl

     signal_var = local_var - (local_mean**2 * noise_var)
     signal_var = np.clip(signal_var, 0, None)

     W = signal_var / (local_mean**2 * noise_var + signal_var)

     return local_mean + W * (image - local_mean)


def compute_histogram(image, num_bins=256, value_range=None):

     counts, bin_edges = np.histogram(image, bins=num_bins, range=value_range)
     probabilities = counts / counts.sum()

     return probabilities, bin_edges


def cumulative_weight_and_means(probabilities, bin_values):

     omega_0 = np.cumsum(probabilities)
     omega_1 = 1 - omega_0
     mu_0 = np.cumsum(bin_values * probabilities) / omega_0
     mu_1 = (np.sum(bin_values * probabilities) - np.cumsum(bin_values * probabilities)) / omega_1

     return omega_0, omega_1, mu_0, mu_1


def otsu_threshold(img, num_bins=256):
     probabilities, bin_edges = compute_histogram(img, num_bins)
     bin_values = (bin_edges[:-1] + bin_edges[1:]) / 2
     omega_0, omega_1, mu_0, mu_1 = cumulative_weight_and_means(probabilities, bin_values)
     variance = omega_0 * omega_1 * (mu_0 - mu_1)**2
     best_t = np.nanargmax(variance)

     return bin_values[best_t]


def apply_threshold(img, threshold):
     water_mask = img <= threshold

     return water_mask, ~water_mask 


def slope_mask(dem, max_slope=5.0, return_slope=False):

     Gx, Gy = np.gradient(dem, axis=1), np.gradient(dem, axis=0)
     
     slope_degrees = np.degrees(np.arctan(np.sqrt(Gx**2 + Gy**2)))

     mask = slope_degrees <= max_slope
     if return_slope:
          return mask, slope_degrees
     return mask


def combine_mask(water_mask, slope_mask):

     return water_mask & slope_mask


def extract_and_save_geojson(flood_extent, transform, crs, min_area_m2=5000):

     points = []

     #extract shape and true/false values
     for shape, value in features.shapes(flood_extent, transform=transform):
          points.append((shape, value))

     #filter out the false values
     true_points = [points[i] for i in range(len(points)) if points[i][1]]

     #save coordinates as a geopandas dataframe
     df = pd.DataFrame()
     df['Coordinates'] = [p[0] for p in true_points]
     df['Coordinates'] = df['Coordinates'].apply(shapely.geometry.shape)

     gdf = gpd.GeoDataFrame(df, geometry='Coordinates', crs=crs)

     #filter out noise
     gdf = gdf[gdf.geometry.area >= min_area_m2]

     gdf['area_ha'] = gdf.geometry.area / 10000 #area stats(hectares)

     #save file
     gdf.to_file('flood_extent.geojson', driver='GeoJSON')

     return gdf


def process_scene(sar_filepath, dem_filepath):

     #load and crop
     sar_array, crs, transform = load_scene(sar_filepath)

     window = Window(col_off = 0, row_off = 500, width = sar_array.shape[1], height = 1500)
     cropped_transform = window_transform(window, transform)
     sar_array = sar_array[500:2000, :]
     transform = cropped_transform

     #despeckle on raw then db conversion
     despeckled = lee_filter(sar_array)
     db_img = to_decibels(despeckled) #scale change

     #find water/land cutoff and apply threshold
     threshold = otsu_threshold(db_img)

     #water mask
     water_mask, land_mask = apply_threshold(db_img, threshold)

     #dem file load and align to sar data
     dem = load_and_align(dem_filepath, transform, sar_array.shape, crs)

     #check if shape match
     print(dem.shape, sar_array.shape)

     #slope mask
     mask_slope, slope = slope_mask(dem, return_slope=True)     

     #combined mask
     comb_mask = combine_mask(water_mask, mask_slope) 

     return comb_mask, transform, crs


def validate_args(args):
     '''
     Checks all CLI arguments for valid values
     '''

     for path, label in [(args.baseline, 'baseline'), (args.flood, 'flood'), (args.dem, 'dem')]:
          if not os.path. exists(path):
               raise FileNotFoundError(f'{label} file not found at: {path}')

          if not path.lower().endswith(('.tif', '.tiff')):
               raise ValueError(f'{label} file should be a .tif/.tiff file, got {path}')

def main():

     parser = argparse.ArgumentParser(description='Flood detection')

     parser.add_argument('baseline', type=str, help='Path to the SAR (.tif/.tiff format) file from before the flooding')
     parser.add_argument('flood', type=str, help='Path to the SAR (.tif/.tiff format) file from during the flooding period')
     parser.add_argument('dem', type=str, help='Path to the DEM file')

     args = parser.parse_args()
     validate_args(args)

     dem_filepath = args.dem

     during_mask, during_transform, during_crs = process_scene(
          args.flood,
          dem_filepath
     )

     baseline_mask, baseline_transform, baseline_crs = process_scene(
          args.baseline,
          dem_filepath
     )

     
     flood_extent = during_mask & ~baseline_mask

     flood_extent_int = flood_extent.astype('uint8')

     #save 
     extract_and_save_geojson(flood_extent_int, during_transform, during_crs)

if __name__ == '__main__':
     main()


     