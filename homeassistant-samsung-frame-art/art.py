import sys
import random
import json
import os
import asyncio
import logging
import argparse
from PIL import Image, ImageOps


sys.path.append('../')

from samsungtvws.async_art import SamsungTVAsyncArt
from samsungtvws import exceptions



logging.basicConfig(level=logging.INFO) #or logging.DEBUG to see messages

def parseargs():
    # Add command line argument parsing
    parser = argparse.ArgumentParser(description='Example async art Samsung Frame TV.')
    parser.add_argument('--ip', action="store", type=str, default=None, help='ip address of TV (default: %(default)s))')
    parser.add_argument('--filter', action="store", type=str, default="none", help='photo filter to apply (default: %(default)s))')
    parser.add_argument('--matte', action="store", type=str, default="none", help='matte to apply (default: %(default)s))')
    parser.add_argument('--matte-color', action="store", type=str, default="black", help='matte color to apply (default: %(default)s))')
    return parser.parse_args()
    



# Set the path to the folder containing the images
folder_path = '/media/frame'

uploaded_json_path = '/data/uploaded.json'


# Load the list of last 5 uploaded pictures
if os.path.exists(uploaded_json_path):
    with open(uploaded_json_path, 'r') as file:
        uploaded_photos = json.load(file)
else:
    uploaded_photos = []

# Get the list of all photos in the folder
files = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]

if len(files) > 5:
    # Exclude the photos on the uploaded list
    available_files = [f for f in files if f not in uploaded_photos]

    # Select a random photo from the available files
    selected_photo = random.choice(available_files)
else:
    selected_photo = random.choice(files)



async def main():
    args = parseargs()


    matte = args.matte
    matte_color = args.matte_color

    # Set the matte and matte color

    if matte != 'none':
        matte_var = f"{matte}_{matte_color}"
    else:
        matte_var = matte



    tv = SamsungTVAsyncArt(host=args.ip, port=8002)
    await tv.start_listening()
    

    supported = await tv.supported()
    if supported:
        logging.info('This TV is supported')

    else:
        logging.info('This TV is not supported')
   
    if supported:
        try:
            #is tv on (calls tv rest api)
            tv_on = await tv.on()
            logging.info('tv is on: {}'.format(tv_on))
            
            #is art mode on
            #art_mode = await tv.get_artmode()                  #calls websocket command to determine status
            art_mode = tv.art_mode                              #passive, listens for websocket messgages to determine art mode status
            logging.info('art mode is on: {}'.format(art_mode))

            #get current artwork
            info = await tv.get_current()
            # logging.info('current artwork: {}'.format(info))
            current_content_id = info['content_id']

            photos = [f for f in os.listdir(folder_path) if f.lower().endswith(('.png', '.jpg'))]
            if not photos:
                logging.info('No PNG or JPG photos found in the folder')
                return
            else:
                filename = selected_photo
                filename = os.path.join(folder_path, filename)
                new_filename = os.path.join(folder_path, os.path.basename(filename).lower())
                os.rename(filename, new_filename)
                filename = new_filename
                logging.info('Selected and renamed photo: {}'.format(filename))


                image = Image.open(filename)
                image = ImageOps.exif_transpose(image)
                new_image = image.resize((3840, 2160))
                new_image.save(filename)



                content_id = None
                if filename:
                    with open(filename, "rb") as f:
                        file_data = f.read()
                    file_type = os.path.splitext(filename)[1][1:] 
                    content_id = await tv.upload(file_data, file_type=file_type, matte=matte_var) 
                    logging.info('uploaded {} to tv as {}'.format(filename, content_id))
                    await tv.set_photo_filter(content_id, args.filter)

                    await tv.select_image(content_id, show=False)
                    logging.info('set artwork to {}'.format(content_id))

               
                    #delete the file that was showing before
                    
                    await tv.delete_list([current_content_id])
                    logging.info('deleted from tv: {}'.format([current_content_id]))  

                    uploaded_photos.append(selected_photo)
                    if len(uploaded_photos) > 5:
                        uploaded_photos.pop(0)
                    
                    with open(uploaded_json_path, 'w') as file:
                        json.dump(uploaded_photos, file)



            await asyncio.sleep(15)

        except exceptions.ResponseError as e:
            logging.warning('ERROR: {}'.format(e))
        except AssertionError as e:
            logging.warning('no data received: {}'.format(e))

        
    await tv.close()


asyncio.run(main())