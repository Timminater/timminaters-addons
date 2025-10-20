import sys
import logging
import os
import json
import argparse
from io import BytesIO
import random

sys.path.append('../')

from samsungtvws import SamsungTVWS
from samsungtvws.exceptions import ResponseError
from sources import bing_wallpapers, google_art, media_folder
from utils.utils import Utils

# Add command line argument parsing
parser = argparse.ArgumentParser(description='Upload images to Samsung TV.')
parser.add_argument('--upload-all', action='store_true', help='Upload all images at once')
parser.add_argument('--debug', action='store_true', help='Enable debug mode to check if TV is reachable')
parser.add_argument('--tvip', help='Comma-separated IP addresses of Samsung Frame TVs')
parser.add_argument('--same-image', action='store_true', help='Use the same image for all TVs (default: different images)')
parser.add_argument('--google-art', action='store_true', help='Download and upload image from Google Arts & Culture')
parser.add_argument('--download-high-res', action='store_true', help='Download high resolution image using dezoomify-rs')
parser.add_argument('--bing-wallpapers', action='store_true', help='Download and upload image from Bing Wallpapers')
parser.add_argument('--media-folder', action='store_true', help='Use images from the local media folder')
parser.add_argument('--debugimage', action='store_true', help='Save downloaded and resized images for inspection')

args = parser.parse_args()

# Set the path to the file that will store the list of uploaded filenames
PREFERRED_DIR = '/share/SamsungFrameTVArtChanger'
FALLBACK_DIR = '/data'


def ensure_upload_dir() -> str:
    for path in (PREFERRED_DIR, FALLBACK_DIR):
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError as err:
            logging.warning(f'Could not create directory {path}: {err}')
    logging.error('No writable directory available for upload list. Using current working directory.')
    return '.'


DATA_DIR = ensure_upload_dir()
upload_list_path = os.path.join(DATA_DIR, 'uploaded_files.json')

# Load the list of uploaded filenames from the file
if os.path.isfile(upload_list_path):
    with open(upload_list_path, 'r') as f:
        uploaded_files = json.load(f)
else:
    uploaded_files = []

# Increase debug level
logging.basicConfig(level=logging.INFO)

sources = []
if args.bing_wallpapers:
    sources.append(bing_wallpapers)
if args.google_art:
    sources.append(google_art)
if args.media_folder:
    sources.append(media_folder)

if not sources:
    logging.error('No image source specified. Please use --google-art, --bing-wallpapers, or --media-folder')
    sys.exit(1)

source_lookup = {
    bing_wallpapers.__name__: bing_wallpapers,
    google_art.__name__: google_art,
    media_folder.__name__: media_folder,
}

tvip = args.tvip.split(',') if args.tvip else []
use_same_image = args.same_image

utils = Utils(args.tvip, uploaded_files)


def remove_uploaded_entry(image_url: str, source_name: str, tv_ip: str) -> None:
    target_ip = tv_ip if len(tvip) > 1 else None
    original_len = len(uploaded_files)
    uploaded_files[:] = [
        entry for entry in uploaded_files
        if not (
            entry.get('file') == image_url
            and entry.get('source') == source_name
            and entry.get('tv_ip') == target_ip
        )
    ]
    if len(uploaded_files) != original_len:
        with open(upload_list_path, 'w') as f:
            json.dump(uploaded_files, f)


def ensure_image_data(
    image_data: BytesIO,
    file_type: str,
    source_name: str,
    image_url: str
):
    if image_data is not None and file_type is not None:
        return image_data, file_type

    source_module = source_lookup.get(source_name)
    if source_module is None:
        logging.error(f'Unknown source "{source_name}", cannot retrieve image data.')
        return None, None

    original_image_data, retrieved_file_type = source_module.get_image(args, image_url)
    if original_image_data is None:
        logging.error(f'Failed to retrieve image data from {source_name} for {image_url}')
        return None, None

    save_debug_image(original_image_data, f'debug_{source_name}_original.jpg')

    logging.info('Resizing and cropping the image...')
    resized_image_data = utils.resize_and_crop_image(original_image_data)

    save_debug_image(resized_image_data, f'debug_{source_name}_resized.jpg')

    return resized_image_data, retrieved_file_type

def process_tv(tv_ip: str, image_data: BytesIO, file_type: str, image_url: str, remote_filename: str, source_name: str):
    tv = SamsungTVWS(tv_ip)

    # Check if TV supports art mode
    if not tv.art().supported():
        logging.warning(f'TV at {tv_ip} does not support art mode.')
        return

    needs_upload = remote_filename is None or args.upload_all

    if remote_filename is not None and not args.upload_all:
        try:
            logging.info(f'Setting existing image on TV at {tv_ip}, skipping upload')
            tv.art().select_image(remote_filename, show=True)
            return
        except ResponseError as err:
            logging.warning(f'Existing image on TV at {tv_ip} unavailable, re-uploading: {err}')
            remove_uploaded_entry(image_url, source_name, tv_ip)
            needs_upload = True
            remote_filename = None
        except Exception as err:
            logging.warning(f'Failed to select existing image on TV at {tv_ip}, re-uploading: {err}')
            remove_uploaded_entry(image_url, source_name, tv_ip)
            needs_upload = True
            remote_filename = None

    if not needs_upload:
        return

    image_data, file_type = ensure_image_data(image_data, file_type, source_name, image_url)
    if image_data is None or file_type is None:
        logging.error(f'Unable to prepare image data for TV at {tv_ip}, skipping upload.')
        return

    # Remove stale entry before uploading a new copy
    if remote_filename is not None:
        remove_uploaded_entry(image_url, source_name, tv_ip)

    try:
        logging.info(f'Uploading image to TV at {tv_ip}')
        remote_filename = tv.art().upload(image_data.getvalue(), file_type=file_type, matte="none")
        if remote_filename is None:
            raise Exception('No remote filename returned')

        tv.art().select_image(remote_filename, show=True)
        logging.info(f'Image uploaded and selected on TV at {tv_ip}')
        # Add the filename to the list of uploaded filenames
        uploaded_files.append({
            'file': image_url,
            'remote_filename': remote_filename,
            'tv_ip': tv_ip if len(tvip) > 1 else None,
            'source': source_name
        })
        # Save the list of uploaded filenames to the file
        with open(upload_list_path, 'w') as f:
            json.dump(uploaded_files, f)
    except Exception as e:
        logging.error(f'There was an error uploading the image to TV at {tv_ip}: {e}')

def get_image_for_tv(tv_ip: str):
    selected_source = random.choice(sources)
    logging.info(f'Selected source: {selected_source.__name__}')

    image_url = selected_source.get_image_url(args)
    remote_filename = utils.get_remote_filename(image_url, selected_source.__name__, tv_ip)

    if remote_filename:
        return None, None, image_url, remote_filename, selected_source.__name__

    image_data, file_type = selected_source.get_image(args, image_url)
    if image_data is None:
        return None, None, None, None, None

    save_debug_image(image_data, f'debug_{selected_source.__name__}_original.jpg')

    logging.info('Resizing and cropping the image...')
    resized_image_data = utils.resize_and_crop_image(image_data)

    save_debug_image(resized_image_data, f'debug_{selected_source.__name__}_resized.jpg')

    return resized_image_data, file_type, image_url, None, selected_source.__name__

def save_debug_image(image_data: BytesIO, filename: str) -> None:
    if args.debugimage:
        with open(filename, 'wb') as f:
            f.write(image_data.getvalue())
        logging.info(f'Debug image saved as {filename}')

if tvip:
    if len(tvip) > 1 and use_same_image:
        selected_source = random.choice(sources)
        logging.info(f'Selected source: {selected_source.__name__}')

        image_url = selected_source.get_image_url(args)
        if not image_url:
            logging.error('No image URL available for same-image mode.')
            sys.exit(1)

        source_name = selected_source.__name__
        remote_filenames = {}
        upload_targets = []

        for tv_ip in tvip:
            remote = utils.get_remote_filename(image_url, source_name, tv_ip)
            if remote:
                remote_filenames[tv_ip] = remote
            else:
                upload_targets.append(tv_ip)

        image_data = None
        file_type = None
        if upload_targets:
            original_image_data, file_type = selected_source.get_image(args, image_url)
            if original_image_data is None:
                logging.error('Failed to retrieve image data for same-image mode.')
                sys.exit(1)

            save_debug_image(original_image_data, f'debug_{source_name}_original.jpg')

            logging.info('Resizing and cropping the image...')
            image_data = utils.resize_and_crop_image(original_image_data)

            save_debug_image(image_data, f'debug_{source_name}_resized.jpg')

        for tv_ip in tvip:
            remote_filename = remote_filenames.get(tv_ip)
            process_tv(tv_ip, image_data, file_type, image_url, remote_filename, source_name)
    else:
        for tv_ip in tvip:
            image_data, file_type, image_url, remote_filename, source_name = get_image_for_tv(tv_ip)
            process_tv(tv_ip, image_data, file_type, image_url, remote_filename, source_name)
else:
    logging.error('No TV IP addresses specified. Please use --tvip')
    sys.exit(1)
