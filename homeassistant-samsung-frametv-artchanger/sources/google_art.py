import logging
import random
import requests
import subprocess
import os
from io import BytesIO
from typing import Optional, Tuple, Union, List, Dict

def get_image_url(args):
    logging.info('Fetching image list from Google Arts & Culture...')
    json_url = "https://www.gstatic.com/culturalinstitute/tabext/imax_2_2.json"
    try:
        response = requests.get(json_url)
        response.raise_for_status()
        image_list: list[dict[str, Union[str, dict]]] = response.json()
        if not image_list:
            raise ValueError("Empty image list received")

        selected_image: dict[str, Union[str, dict]] = random.choice(image_list)
        return f"https://artsandculture.google.com/{selected_image['link']}"
    except (requests.RequestException, ValueError, KeyError) as e:
        logging.error(f"Error getting image url: {str(e)}")
        return None

def get_image(args, image_url) -> Tuple[Optional[BytesIO], Optional[str]]:
    download_high_res = args.download_high_res

    if download_high_res:
        logging.info(f'Downloading high-res image from {image_url}')
        output_file: str = "temp.jpg"

        try:
            subprocess.run(["dezoomify-rs", "--max-width", "5001", "--compression", "0", image_url, output_file], check=True)
            with open(output_file, 'rb') as f:
                image_data: BytesIO = BytesIO(f.read())
            os.remove(output_file)  # Clean up the temporary file
            return image_data, 'JPEG'
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to download high-res image: {str(e)}")
            return None, None
        except OSError as e:
            logging.error(f"Failed to remove temporary file: {str(e)}")
            # Continue execution even if file removal fails
    else:
        try:
            image_url = image_url + "=w3840-h2160-c"
            logging.info(f'Downloading image from {image_url}')
            image_response = requests.get(image_url)
            image_response.raise_for_status()
            image_data = BytesIO(image_response.content)
        
            return image_data, 'JPEG'
        except (requests.RequestException, ValueError, KeyError) as e:
            logging.error(f"Error getting image: {str(e)}")
            return None, None