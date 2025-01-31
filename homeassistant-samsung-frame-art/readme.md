# Samsung Frame Art Add on for Home Assistant

![TV with some art on it ](https://i.imgur.com/BunHdwb.jpeg)

This add-on uses the awesomework of <https://github.com/ow/samsung-frame-art> . It adds the ability to place images in "Media" and it will randomly change the image on your Samsung the Frame

By starting the add-on it will randomly pick a image from te folder and put it on your frame; after the image is placed the add-on will automatically stop again. This for instance can be triggered via an automation to it on a daily basis. Please find an example below.

The only configuration option is setting the IP adress of your Samsung The Frame

When you start the addon for the first time it creates a specific directory Media/ MyMedia called "frame" where you can place your images.

Please note (I've found this the hard way!) just must upload pictures with a lower-case extension and only .png and .jpg

Install this addon by adding the repository:

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fgijsvdhoven%2Fhomeassistant-addons)


```
description: ""
mode: single
trigger:
  - platform: time
    at: "23:00:00"
condition: []
action:
  - service: hassio.addon_start
    data:
      addon: local_hass-frame-changer
```
