# python
#
# This file is part of the mqttDisplayClient distribution
# (https://github.com/olialb/mqttDisplayClient).
# Copyright (c) 2025 Oliver Albold.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
"""
Module implements a MQTT client for FullPageOS
"""

import configparser
import json
import subprocess
import threading
import os
import signal
import sys
import serial  # Added for LD2450
import time
# used to validate URLs:
import validators
import gpiozero
from chrome_tab_api import ChromeTabAPI
from base_mqtt_client import base_mqtt_client as BMC
from paho.mqtt import client as mqtt_client
from ld2450 import *
import board
import busio
#
# global constants
#
CONFIG_FILE = "mqttDisplayClient.ini"  # name of the ini file
PANEL_DEFAULT = (
    "DEFAULT"  # keyword to set the website back to the configured FullPageOS default
)
PANEL_SHOW_URL = "URL"  # keyword to set website as panel set over url topic
PANEL_BLANK_URL = "about:blank"  # URL of the blank web page
PANEL_BLANK = "BLANK" #show a blank panel
PANEL_RELOAD = "RELOAD" #reload current panel
IDLE = ">_"
LOG_ROTATE_WHEN='midnight'
LOG_BACKUP_COUNT=5
LOG_FILE_PATH="log"
LOG_FILE_NAME=None
LOG_FILE_HANDLER = None
# LD2450 
REPORT_HEADER = b'\xAA\xFF\x03\x00' #ld2450
REPORT_TAIL = b'\x55\xCC'           #ld2450
# LD2450 Commands
CMD_HEADER = b'\xFD\xFC\xFB\xFA'
CMD_FOOTER = b'\x04\x03\x02\x01'
CMD_ENABLE_CONF = b'\x00\xFF'
CMD_DISABLE_CONF = b'\x00\xFE'
CMD_SET_BAUD    = b'\x00\xA1'
CMD_READ_VER    = b'\x00\xA0'
# import from the configuration file only the feature configuraion
FEATURE_CFG = configparser.ConfigParser()

# try to open ini file
try:
    if os.path.exists(CONFIG_FILE) is True:
        FEATURE_CFG.read(CONFIG_FILE)
except OSError:
    print(f"Error while reading ini file: {CONFIG_FILE}")
    sys.exit()

# read features status
BACKLIGHT = False
if "backlight" in FEATURE_CFG["feature"]:
    if FEATURE_CFG["feature"]["backlight"].upper() == "ENABLED":
        BACKLIGHT = True

PYAUTOGUI = False
if "pyautogui" in FEATURE_CFG["feature"]:
    if FEATURE_CFG["feature"]["pyautogui"].upper() == "ENABLED":
        PYAUTOGUI = True

LD2450 = False
if "ld2450" in FEATURE_CFG["feature"]:
    if FEATURE_CFG["feature"]["ld2450"].upper() == "ENABLED":
        LD2450 = True

LD2450_2 = False
if "ld2450_2" in FEATURE_CFG["feature"]:
    if FEATURE_CFG["feature"]["ld2450_2"].upper() == "ENABLED":
        LD2450_2 = True

# import the functions zo control the display with autogui
if PYAUTOGUI:
    os.environ["DISPLAY"] = ":0"  # environment variable needed for pyautogui
    import pyautogui

    # local imports:
    from autogui_commands import call_autogui_cmd_list, autogui_log



#
# define main class
#
class MqttDisplayClient(BMC.BaseMqttClient): # pylint: disable=too-many-instance-attributes
    """
    Main class of MQTT display client for FullPageOS
    """
    def __init__(self, config_file):
        """
        Constructor takes config file as parameter (ini file) and defines global atrributes
        """
        # other global attributes
        self.default_url_file = None  # default FullPageOS config file for url
        self.display_id = None  # Touch display ID
        self.brightness = -1  # display brightness which was last published
        self.backlight = None  # backlight status
        self.backlight_published = None  # backlight status which was last published
        self.published_url = None  # url which was last published to broker
        self.autogui_feedback = "OK"  # feedback on last macro call
        self.autogui_feedback_published = (
            None  # last pubished    feedback on last macro call
        )
        self.autogui_commands = (
            None  # commands which will be performt when current website is loaded
        )
        self.current_panel = PANEL_DEFAULT  # Panel which is currently shown
        self.current_panel_published = None  # Panel which was last publised to broker
        self.reserved_panel_names = [PANEL_DEFAULT, PANEL_SHOW_URL, PANEL_BLANK, PANEL_RELOAD]
        self.shell_cmd = IDLE
        self.published_shell_cmd = None
        #chrome api attributes
        self.chrome_pages = None
        self.chrome_port = 9222
        self.chrome_tab_timeout = 600
        self.chrome_reload_timeout = 3600
        self.chrome_topic = False

        # Multi-sensor state management (refactored)
        self.off_delay = 120  # delay per sensor

        self.ld_sensors = {
            1: {
                "occupied": False,              # raw state
                "occupied_delayed": False,      # delayed state (per sensor)
                "timer": None,                  # per sensor timer
                "last_count": -1,
                "pub_data": None,
                "data": {
                    "occupied": False,
                    "occupied_delayed": False,
                    "target_count": 0,
                    "targets": []
                }
            },
            2: {
                "occupied": False,
                "occupied_delayed": False,
                "timer": None,
                "last_count": -1,
                "pub_data": None,
                "data": {
                    "occupied": False,
                    "occupied_delayed": False,
                    "target_count": 0,
                    "targets": []
                }
            }
        }


        self.mqtt_lock = threading.Lock() # Prevents MQTT collisions
        self.start_time = time.time()

        # Global config:
        BMC.BaseMqttClient.__init__(self, config_file)
        # Define lwt status topic
        status_topic = f"{self.topic_root}/status"
        # Set the Will: payload "OFFLINE", retain=True so new clients see the status
        #self.client.will_set(status_topic, payload="OFFLINE", qos=1, retain=True)
        #read default config of FullPageOS
        self.read_default_url()
        #after default url of FullPageOS is known add it to topic config
        self.topic_config["panel"]["panels"][PANEL_DEFAULT] = self.default_url
        self.topic_config["panel"]["panels"][PANEL_SHOW_URL] = self.default_url
        self.topic_config["panel"]["panels"][PANEL_BLANK] = PANEL_BLANK_URL
        # I2C sensors (must always exist as attributes)
        self.i2c = None
        self.bme280 = None
        self.bh1750 = None
        self._bme280_last = 0
        self._bh1750_last = 0

    def init_i2c_sensors(self):
        try:
            import adafruit_bme280.basic as adafruit_bme280
            import adafruit_bh1750

            i2c = busio.I2C(board.SCL, board.SDA)

            # BME280
            cfg = self.topic_config["bme280"]
            if cfg.get("enabled"):
                self.bme280 = adafruit_bme280.Adafruit_BME280_I2C(
                    i2c,
                    address=cfg["address"]
                )
                # Store interval
                self.bme280_interval = int(cfg.get("interval", 15))
                # Apply offsets
                self.bme280.temperature_offset = float(cfg.get("temperature_offset", 0.0))
                self.bme280.humidity_offset = float(cfg.get("humidity_offset", 0.0))
                self.bme280.pressure_offset = float(cfg.get("pressure_offset", 0.0))  
                self.log.info("BME280 initialized at 0x%02X", cfg["address"])

            # BH1750
            cfg = self.topic_config["bh1750"]
            if cfg.get("enabled"):
                self.bh1750 = adafruit_bh1750.BH1750(i2c)
                self.bh1750.multiplikator = float(cfg.get("multiplikator", 0.0))
                self.log.info("BH1750 initialized at 0x%02X", cfg["address"])

        except Exception as e:
            self.log.warning("I2C sensor init failed: %s", e)


    def init_chrome_api( self, config ):
        """Chreate to class for the chrome api"""
        # read chrome config
        try:
            self.chrome_port = config["chrome"]["port"]
            self.chrome_tab_timeout = int(config["chrome"]["pageTimeout"])
            self.chrome_reload_timeout = int(config["chrome"]["reloadTimeout"])
        except (KeyError, ValueError):
            self.log.warning("[chrome] section not specified in ini file! Default values used" )

        try:
            self.chrome_max_tabs = int(config["chrome"]["maxTabs"])
        except (KeyError, ValueError):
            self.chrome_max_tabs = 0
            self.log.warning("maxTabs in [chrome] section not specified. Set to 0." )
        
        self.chrome_pages = ChromeTabAPI(
            self.publish_delay,
            self.chrome_port,
            (self.chrome_tab_timeout, self.chrome_reload_timeout),
            self.chrome_max_tabs
        )
        self.chrome_pages.set_log(self.log_level, self.log_file_handler)
        self.chrome_pages.sync()
        self.chrome_pages.set_reload_callback( self.autogui_panel_cmds )

    def _get_readable_uptime(self):
        """Calculates and formats the system uptime."""
        uptime_seconds = int(time.time() - self.start_time)
        days, rem = divmod(uptime_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)

        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {seconds}s"

    def _set_ld2450_command(self, sensor_id, my_config, msg):
        """Handle MQTT commands for a specific LD2450 sensor (by sensor_id)"""
        if not LD2450:
            return

        try:
            LD2450_SECTION_MAP = {
                1: "ld2450",
                2: "ld2450_2",
            }

            # Extract command text
            if isinstance(msg, dict):
                cmd_text = str(msg.get("msg", "")).upper().strip()
            else:
                cmd_text = str(msg).upper().strip()

            if not cmd_text:
                return

            section = LD2450_SECTION_MAP.get(sensor_id)
            if not section:
                self.log.error(f"Invalid LD2450 sensor id: {sensor_id}")
                return

            port = my_config.get("port")
            baud = my_config.get("baudrate", 256000)

            if not port:
                self.log.error(f"{section}: missing serial port in config")
                return

            with serial.Serial(port, baud, timeout=1) as ser:
                self.log.info(
                    f"LD2450[{sensor_id}] ({section} @ {port}) command: {cmd_text}"
                )

                if not enable_configuration_mode(ser):
                    self.log.error(f"LD2450[{sensor_id}] failed to enter config mode")
                    return

                if cmd_text == "VERSION":
                    version = read_firmware_version(ser)
                    self.log.info(f"LD2450[{sensor_id}] firmware: {version}")

                elif cmd_text == "REBOOT":
                    restart_module(ser)

                elif cmd_text == "SINGLE":
                    single_target_tracking(ser)

                elif cmd_text == "MULTI":
                    multi_target_tracking(ser)

                elif cmd_text == "QUERY":
                    mode = query_target_tracking(ser)
                    self.log.info(f"LD2450[{sensor_id}] tracking mode: {mode}")

                elif cmd_text.startswith("BAUD"):
                    parts = cmd_text.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        set_serial_port_baud_rate(ser, int(parts[1]))
                    else:
                        self.log.error("Invalid BAUD command format")

                elif cmd_text == "FACTORY_RESET":
                    restore_factory_settings(ser)

                elif cmd_text == "BT_ON":
                    bluetooth_setup(ser, True)

                elif cmd_text == "BT_OFF":
                    bluetooth_setup(ser, False)

                else:
                    self.log.warning(f"Unknown LD2450 command: {cmd_text}")

                end_configuration_mode(ser)

        except Exception as e:
            self.log.error(f"LD2450[{sensor_id}] error: {e}")




    def _publish_ld2450_config(self, topic, my_config):
        pass

    def _publish_bme280(self, topic, cfg):
        if not self.bme280 or not cfg.get("enabled"):
            return

        now = time.time()
        if now - self._bme280_last < cfg["interval"]:
            return
        self._bme280_last = now
        # read offsets from config (default to 0 if missing)
        temp_offset = float(self.topic_config['bme280']['temperature_offset']) 
        hum_offset = float(self.topic_config['bme280']['humidity_offset'])
        press_offset = float(self.topic_config['bme280']['pressure_offset'])
        # apply offsets
        temperature = self.bme280.temperature + temp_offset
        humidity = self.bme280.humidity + hum_offset
        pressure = self.bme280.pressure + press_offset

        self.client.publish(f"{topic}/temperature", round(temperature, 2))
        self.client.publish(f"{topic}/humidity", round(humidity, 2))
        self.client.publish(f"{topic}/pressure", round(pressure, 2))


    def _publish_bh1750(self, topic, cfg):
        if not self.bh1750 or not cfg.get("enabled"):
            return

        now = time.time()
        if now - self._bh1750_last < cfg["interval"]:
            return
        self._bh1750_last = now
        lightraw = float(self.topic_config['bh1750']['multiplikator'])
        # apply multiplicator
        light = self.bh1750.lux * lightraw
        self.client.publish(f"{topic}/lux", round(light, 2))




    def read_client_config(self, config):
        """
        Reads the configured ini file and sets attributes based on the config
        """
        # topic configuration
        self.topic_config = {
            #"brightness": {
            #    "topic": "brightness_percent",
            #    "publish": self._publish_brightness,
            #    "set": self._set_brightness,
            #},
            "backlight": {
                "topic": "backlight",
                "publish": self._publish_backlight
            },
            "system": {"topic": "system", "publish": self._publish_system},
            "shell": {
                "topic": "shell",
                "publish": self._publish_shell_cmd,
                "set": self._set_shell_cmd,
            },
            "url": {"topic": "url", "publish": self._publish_url, "set": self._set_url},
            "panel": {
                "topic": "panel",
                "publish": self._publish_panel,
                "set": self._set_panel,
            },
            "autogui": {
                "topic": "autogui",
                "publish": self._publish_autogui_results,
                "set": self._set_autogui,
            },
            "chrome": {"topic": "chrome", "publish": self._publish_chrome},
        }

        # Config for LD2450 #1
        self.topic_config["ld2450"] = {
            "topic": "ld2450",
            "publish": lambda t, c: self._publish_ld2450(t, 1),
            "set": lambda c, m: self._set_ld2450_command(1, c, m),
            "port": config["ld2450"].get("port", "/dev/serial0"),
            "baudrate": int(config["ld2450"].get("baudrate", "256000"))
        }

        # Config for LD2450 #2 (USB)
        if LD2450_2:
            self.topic_config["ld2450_2"] = {
                "topic": "ld2450_2",
                "publish": lambda t, c: self._publish_ld2450(t, 2),
                "set": lambda c, m: self._set_ld2450_command(2, c, m),
                "port": config["ld2450_2"].get("port", "/dev/ttyUSB0"),
                "baudrate": int(config["ld2450_2"].get("baudrate", "256000"))
            }

        self.topic_config.update({
            "bme280": {
                "topic": "bme280",
                "publish": self._publish_bme280
            },
            "bh1750": {
                "topic": "bh1750",
                "publish": self._publish_bh1750
            }
        })

        # read ini file values
        try:
            # set loglevel of autogui
            if PYAUTOGUI is True:
                autogui_log(self.log_level, self.log_file_handler) #pylint: disable=possibly-used-before-assignment

            # read server config
            self.display_id = config["global"]["displayID"]
            self.default_url_file = config["global"]["defaultUrl"]

            # read mqtt topic config brighness
            #self.topic_config["brightness"]["min"] = int(config["brightness"]["min"])
            #self.topic_config["brightness"]["max"] = int(config["brightness"]["max"])
            #self.topic_config["brightness"]["cmd"] = config["brightness"]["set"]
            #self.topic_config["brightness"]["get"] = config["brightness"]["get"]

            # read mqtt topic config backlight
            self.topic_config["backlight"]["ON"] = config["backlight"]["ON"]
            self.topic_config["backlight"]["OFF"] = config["backlight"]["OFF"]
            #self.topic_config["backlight"]["cmd"] = config["backlight"]["set"]
            #self.topic_config["backlight"]["get"] = config["backlight"]["get"]

            # read mqtt topic config ld2450
            #self.topic_config["ld2450"]["port"] = config["ld2450"].get("port", "/dev/serial0")
            #self.topic_config["ld2450"]["baudrate"] = int(config["ld2450"].get("baudrate", "256000"))
            #self.topic_config["ld2450"]["cmd_set"] = config["ld2450"].get("set", "")

            # ---- BME280 config ----
            if "bme280" in config:
                self.topic_config["bme280"]["enabled"] = config["bme280"].getboolean("enabled", True)
                self.topic_config["bme280"]["i2c_bus"] = int(config["bme280"].get("i2c_bus", 1))
                self.topic_config["bme280"]["address"] = int(config["bme280"].get("address", "0x76"), 16)
                self.topic_config["bme280"]["interval"] = int(config["bme280"].get("interval", 15))

                self.topic_config["bme280"]["temperature_offset"] = float(config["bme280"].get("temperature_offset", 0.0))
                self.topic_config["bme280"]["humidity_offset"] = float(config["bme280"].get("humidity_offset", 0.0))
                self.topic_config["bme280"]["pressure_offset"] = float(config["bme280"].get("pressure_offset", 0.0))

            else:
                self.topic_config["bme280"]["enabled"] = False

            # ---- BH1750 config ----
            if "bh1750" in config:
                self.topic_config["bh1750"]["enabled"] = config["bh1750"].getboolean("enabled", True)
                self.topic_config["bh1750"]["i2c_bus"] = int(config["bh1750"].get("i2c_bus", 1))
                self.topic_config["bh1750"]["address"] = int(config["bh1750"].get("address", "0x23"), 16)
                self.topic_config["bh1750"]["interval"] = int(config["bh1750"].get("interval", 10))
                self.topic_config["bh1750"]["multiplikator"] = float(config["bh1750"].get("multiplikator", 0.0))
            else:
                self.topic_config["bh1750"]["enabled"] = False


            # read config system commands
            self.topic_config["shell"]["commands"] = {}
            for key, cmd in config.items("shellCommands"):
                self.topic_config["shell"]["commands"][key.upper()] = cmd

            #create chrome Page class
            self.init_chrome_api(config)

            #check if special chrome topic should be enabled
            if 'chromeTopic' in config["logging"]:
                self.chrome_topic = config["logging"]["chromeTopic"]

            # read configured panels
            sites_items = config.items("panels")
            self.topic_config["panel"]["panels"] = {}
            for key, panel in sites_items:
                if key in self.reserved_panel_names:
                    raise RuntimeError(f"Reserved panel name not allowed: {key}")
                s_list = panel.split("|")
                if validators.url(s_list[0]) is True:
                    self.topic_config["panel"]["panels"][key.upper()] = panel
                else:
                    raise RuntimeError(f"Configured URL not well formed: {key}={s_list[0]}")

            if LD2450:
                threading.Thread(target=self._ld2450_read_thread, args=(1,), daemon=True).start()
            if LD2450_2:
                threading.Thread(target=self._ld2450_read_thread, args=(2,), daemon=True).start()
                
        except (KeyError, RuntimeError) as error:
            self.log.error("Error while reading ini file: %s", error)
            sys.exit()


    def _ld2450_read_thread(self, sensor_id):
        """Generic background thread for LD2450 sensors."""
        cfg_key = "ld2450" if sensor_id == 1 else "ld2450_2"
        try:
            cfg = self.topic_config[cfg_key]
            port, baud = cfg["port"], cfg["baudrate"]
            ser = serial.Serial(port, baud, timeout=0.01)
            self.log.info(f"LD2450_{sensor_id} running on {port}")
            
            while True:
                if ser.in_waiting >= 30:
                    data = ser.read_until(REPORT_TAIL)
                    if REPORT_HEADER in data:
                        start_idx = data.find(REPORT_HEADER) + len(REPORT_HEADER)
                        payload = data[start_idx : start_idx + 24]
                        if len(payload) == 24:
                            self._parse_ld2450(sensor_id, payload)
                else:
                    time.sleep(0.005) 
        except Exception as e:
            self.log.error(f"LD2450_{sensor_id} Thread Crash: {e}")

    def _parse_ld2450(self, sensor_id, data):

        s = self.ld_sensors[sensor_id]
        targets = []

        # -------- Parse Targets --------
        for i in range(3):
            offset = i * 8
            x = int.from_bytes(data[offset:offset+2], byteorder='little', signed=True)
            y = int.from_bytes(data[offset+2:offset+4], byteorder='little', signed=True)

            if y != 0:
                distance = (x**2 + y**2)**0.5
                targets.append({
                    "id": i + 1,
                    "x_mm": x,
                    "y_mm": y,
                    "dist_mm": round(distance, 1)
                })

        new_count = len(targets)
        new_raw_state = new_count > 0

        significant_change = False

        # Detect count change
        if new_count != s["last_count"]:
            significant_change = True

        # -------- RAW STATE CHANGE --------
        if new_raw_state != s["occupied"]:

            s["occupied"] = new_raw_state
            significant_change = True

            # -------- DISPLAY CONTROL (GLOBAL RAW LOGIC) --------
            any_raw_occupied = any(
                sensor["occupied"] for sensor in self.ld_sensors.values()
            )

            cmd = "displayein" if any_raw_occupied else "displayaus"
            self._set_shell_cmd(self.topic_config["shell"], cmd)

            # -------- DELAYED LOGIC (PER SENSOR) --------
            if new_raw_state:
                # Cancel delayed OFF timer
                if s["timer"]:
                    s["timer"].cancel()
                    s["timer"] = None

                if not s["occupied_delayed"]:
                    s["occupied_delayed"] = True
                    significant_change = True

            else:
                # Start delayed OFF timer
                if s["timer"]:
                    s["timer"].cancel()

                s["timer"] = threading.Timer(
                    self.off_delay,
                    self._delayed_off,
                    args=(sensor_id,)
                )
                s["timer"].start()

        # -------- Update Data Structure --------
        s["data"] = {
            "occupied": s["occupied"],
            "occupied_delayed": s["occupied_delayed"],
            "target_count": new_count,
            "targets": targets
        }

        # -------- Publish --------
        if significant_change:
            s["last_count"] = new_count
            suffix = "" if sensor_id == 1 else "_2"
            self._publish_ld2450(f"{self.topic_root}/ld2450{suffix}", sensor_id)

    def _delayed_off(self, sensor_id):
        """Called when a sensor's delayed timer expires"""

        s = self.ld_sensors[sensor_id]
        s["occupied_delayed"] = False
        s["timer"] = None

        self.log.info(f"LD2450[{sensor_id}] delayed occupancy OFF")

        suffix = "" if sensor_id == 1 else "_2"
        self._publish_ld2450(f"{self.topic_root}/ld2450{suffix}", sensor_id)

    def _publish_ld2450(self, topic, sensor_id):
        s = self.ld_sensors[sensor_id]
        payload = json.dumps(s["data"])
        if payload != s["pub_data"]:
            if self.client.publish(topic, payload).rc == 0:
                s["pub_data"] = payload
  
    def connect(self):
        """
        Overrides the base connect to set the LWT after object creation 
        but before the network connection is made.
        """
        # 1. Create the actual MQTT client object using the newly imported name
        self.client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
        
        # 2. Set the Last Will and Testament (LWT)
        # This MUST happen here, before the network connect call
        status_topic = f"{self.topic_root}/status"
        self.log.info("Setting LWT to topic: %s", status_topic)
        self.client.will_set(status_topic, payload="OFFLINE", qos=1, retain=True)

        # 3. Setup credentials
        if self.username != "":
            self.client.username_pw_set(self.username, self.password)
        
        # 4. Set callbacks
        self.client.on_connect = BMC.BaseMqttClient.on_connect
        self.client.on_disconnect = BMC.BaseMqttClient.on_disconnect
        self.client.user_data_set(self)

        # 5. Establish the connection
        while True:
            try:
                self.client.connect(self.broker, self.port)
                # Once connected, immediately announce ONLINE status
                self.client.publish(status_topic, "ONLINE", retain=True)
                break
            except OSError as error:
                self.log.warning("Connection failed: %s. Retrying...", error)
                import time # Locally imported to ensure it's available
                time.sleep(self.reconnect_delay)

        # 6. Start the background loop
        self.client.loop_start()

    def read_default_url(self):
        """
        Reads configures default url of FullPageOS
        """
        # read the default page from FullPageOS
        try:
            with open(self.default_url_file, "r", encoding="utf-8") as f:
                self.default_url = str(f.read()).strip()
                if validators.url(self.default_url) is False:
                    self.log.warning(
                        "FullPageOS default page has not a well formend URL format: %s",
                        self.default_url
                    )
            self.log.info("FullPageOS default page: %s", self.default_url)
        except OSError as error:
            self.log.error("Error while reading FullPageOS web page config: %s", error)
            sys.exit()

    def thread_autogui_func(self, cmds):
        """
        Thread which is executing a string with autogui commands
        """
        # excecute autogui commands with this website
        cmds = cmds.strip()
        feedback = call_autogui_cmd_list(cmds) # pylint: disable=possibly-used-before-assignment
        if feedback == "OK":
            self.log.info("Command list excecuted without error: '%s'",cmds)
        else:
            self.log.warning("Command list excecuted with error: '%s'", feedback)
        self.autogui_feedback = feedback

    def call_autogui_commands(self, cmds):
        """
        Starts a thread with is excecuting autogui commands from a string
        parallel to the client.
        """
        if self.autogui_feedback[0 : len("EXEC")] == "EXEC":
            self.log.warning("Thread allready running can not excecute: '%s'",cmds)
            return
        self.autogui_feedback = "EXEC: " + cmds
        # create thread
        params = [cmds]
        thread = threading.Thread(target=self.thread_autogui_func, args=params)
        # run the thread
        thread.start()

    def autogui_panel_cmds( self ):
        """call back to perform autogui commands assigned to current panel"""
        if self.autogui_commands is not None and PYAUTOGUI is True:
            self.call_autogui_commands( self.autogui_commands )

    def _set_website(self, url):
        """
        helper method to set an url in the browser
        """
        # set a defined given website
        return self.chrome_pages.activate_tab ( url )

    #def _set_brightness(self, my_config, msg):
    #    """
    #    mqtt command to set the brightness
    #    """
    #    if BACKLIGHT is False:
    #        # feature is switched off
    #        self.log.warning(
    #            "Error brightness command received but backlight feature is not enabled!"
    #        )
    #        return
    #    # Synax OK we can call the command to set the brightness
    #    msg = msg.strip()
    #    bmin = my_config["min"]
    #    bmax = my_config["max"]
    #    try:
    #        value = int((float(msg)) / (100 / (bmax - bmin))) + bmin
    #        value = min(bmax, max( bmin, value ))
    #    except ValueError as error:
    #        self.log.warning("Error in brightness payload %s: %s", msg, error)
    #        return

    #    # call command to set the brightness
    #    self.log.debug("Call: %s",my_config["cmd"].format(value=value, displayID=self.display_id))
    #    err, msg = subprocess.getstatusoutput(
    #        my_config["cmd"].format(value=value, displayID=self.display_id)
    #    )
    #    if err != 0:
    #        self.log.error("Error %s executing command: %s", err, msg)

    def _set_backlight(self, my_config, msg):
        """
        mqtt command to switch the backlight on and off
        """
        if BACKLIGHT is False:
            # feature is switched off
            self.log.warning("Error backlight command received but feature is not enabled!")
            return
        # Synax OK we can call the command to set the backlight status
        msg = msg.strip()
        if msg.upper() == "ON" or msg.upper() == "OFF":
            value = my_config[msg]
        else:
            self.log.warning("Error in backlight payload: %s", msg)
            return

        # call command to set the backlight
        if msg != self.backlight:
            self.log.debug(my_config["cmd"].format(value=value, displayID=self.display_id))
            err, ret = subprocess.getstatusoutput(
                my_config["cmd"].format(value=value, displayID=self.display_id)
            )
            if err != 0:
                self.log.error("Error %s executing command: %s", err, ret)
            else:
                self.backlight = msg

    def thread_shell_cmd_func(self, cmd):
        """
        thread which executes a shell command in parallel to the client
        """
        # excecute system cmd
        err, msg = subprocess.getstatusoutput(cmd)
        if err != 0:
            self.log.error("Error %s executing command: %s", err, msg)
            self.shell_cmd = IDLE
        else:
            self.shell_cmd = IDLE

    def _set_shell_cmd(self, my_config, msg):
        """
        mqtt command to execute a shell command in a parallel thread.
        Now explicitly handles DISPLAYEIN and DISPLAYAUS to sync backlight state.
        """
        msg = msg.strip().upper()
        if msg in my_config["commands"]:
            if self.shell_cmd != IDLE:
                # currently is another command running. Skip this command
                self.log.warning("Shell command already running skip: %s", msg)
                return
            
            # --- BACKLIGHT SYNC LOGIC ---
            if BACKLIGHT:
                target_state = None
                if msg == "DISPLAYEIN":
                    target_state = "ON"
                elif msg == "DISPLAYAUS":
                    target_state = "OFF"
                
                if target_state:
                    # Update internal tracking
                    self.backlight = target_state
                    # Trigger immediate publish with the forced value
                    backlight_topic = f"{self.topic_root}/{self.topic_config['backlight']['topic']}"
                    self._publish_backlight(backlight_topic, self.topic_config["backlight"], force_value=target_state)
            
            # call the configured command
            self.log.debug("Call command: %s", my_config["commands"][msg])
            self.shell_cmd = msg
            
            # publish that the command is now executed
            self._publish_shell_cmd(
                self.topic_root + "/" + my_config["topic"], my_config
            )
            

            # prepare thread
            params = [my_config["commands"][msg]]
            thread = threading.Thread(target=self.thread_shell_cmd_func, args=params)
            # run the thread
            thread.start()
        else:
            self.log.info("Unknown command payload received: '%s'", msg)

    def _set_url(self, my_config, msg): # pylint: disable=unused-argument
        """
        mqtt command to set an individual url
        """
        msg = msg.strip()
        if not validators.url(msg):
            self.log.info("Received url has no valid format: '%s'", msg)
            return

        # set the new url in browser:
        if self._set_website( msg ) is not True:
            self.log.warning("Received url could not be opened: '%s'", msg)
        else:
            self.autogui_commands = None
            self.topic_config["panel"]["panels"][PANEL_SHOW_URL] = msg

    def _set_panel(self, my_config, msg):
        """
        mqtt command to set one of the configured panel urls
        """
        msg = msg.strip()
        newsite = None
        if msg.upper() in my_config["panels"]:
            definition = my_config["panels"][msg.upper()]
            self.current_panel = msg.upper()
            # does the definition contain autogui commands?
            index = definition.find("|")
            if index > 0:
                self.autogui_commands = definition[index + 1 :]
                newsite = definition[0:index]
            else:
                newsite = definition
                self.autogui_commands = None
        else:
            self.log.info("Received panel name is not configured: '%s'", msg.upper())
            return

        # set the new url in browser:
        if self._set_website ( newsite ) is True:
            if self.autogui_commands is not None and PYAUTOGUI is True:
                self.call_autogui_commands(self.autogui_commands)
        else:
            self.log.error("Panel could not be activated: '%s'", msg.upper())

    def _set_autogui(self, my_config, msg): # pylint: disable=unused-argument
        """
        mqtt command to execute a list of autogui commands from a string
        """
        if PYAUTOGUI is True:
            self.call_autogui_commands(msg)

    def _get_memory_entities(self):
        meminfo = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                key, value = line.split(":", 1)
                meminfo[key.strip()] = int(value.strip().split()[0])  # kB

        # RAM
        ram_total = meminfo.get("MemTotal", 0) / 1024       # MB
        ram_available = meminfo.get("MemAvailable", 0) / 1024
        ram_used = ram_total - ram_available

        # Swap
        swap_total = meminfo.get("SwapTotal", 0) / 1024     # MB
        swap_free = meminfo.get("SwapFree", 0) / 1024
        swap_used = swap_total - swap_free

        # Build dictionary
        memory = {
            "ram_total_mb": round(ram_total, 0),
            "ram_used_mb": round(ram_used, 0),
            "ram_available_mb": round(ram_available, 0),
            "swap_total_mb": round(swap_total, 0),
            "swap_used_mb": round(swap_used, 0),
            "swap_free_mb": round(swap_free, 0)
        }
        return memory



    def _publish_system(self, topic, my_config): # pylint: disable=unused-argument
        """
        publish the system topic
        """
        loadavg_one, loadavg_five, loadavg_fifteen = os.getloadavg()
        # collect system info
        system_info = {}
        system_info["chrome_tabs"] = self.chrome_pages.tab_count()
        system_info["memory"] = self._get_memory_entities()
        system_info["uptime"] = self._get_readable_uptime()  
        system_info["cpu_temp"] = round(gpiozero.CPUTemperature().temperature, 2)
        system_info["cpu_load"] = round(gpiozero.LoadAverage().load_average, 2)
        system_info["cpu_load_1min"] = round(loadavg_one, 2)
        system_info["cpu_load_5min"] = round(loadavg_five, 2)
        system_info["cpu_load_15min"] = round(loadavg_fifteen, 2)
        system_info["disk_usage"] = round(gpiozero.DiskUsage().usage, 2)
        if PYAUTOGUI is True:
            system_info["mouse_position"] = pyautogui.position() # pylint: disable=possibly-used-before-assignment
            system_info["display_size"] = pyautogui.size()
        #system_info["default_url"] = self.default_url
        # create a json out of it
        msg = json.dumps(system_info)
        # send message to broker
        result = self.client.publish(topic, msg)
        # result: [0, 1]
        status = result[0]
        if status == 0:
            self.log.debug("Send '%s' to topic %s", msg, topic)
        else:
            self.log.error("Failed to send message to topic %s", topic)

    def _publish_chrome(self, topic, my_config): # pylint: disable=unused-argument
        """
        publish the chrome topic
        """
        #check if chrome topic is enabled
        if self.chrome_topic != "true":
            return
        # collect system info
        chrome = {}
        active = self.chrome_pages.active()
        chrome ["active_id"] = active.id()
        chrome ["active_url"] = active.url()
        chrome ["reload"] = self.chrome_pages.focus_reload
        tabs = self.chrome_pages.tabs()
        chrome["tabs"] = {}
        for t_id, tab in tabs.items():
            jt = {}
            jt["url"] = tab.url()
            jt["timeout"] = self.chrome_pages.get_timeout(tab)
            chrome["tabs"][t_id] = jt
        # create a json out of it
        msg = json.dumps(chrome)
        # send message to broker
        result = self.client.publish(topic, msg)
        # result: [0, 1]
        status = result[0]
        if status == 0:
            self.log.debug("Send '%s' to topic %s", msg, topic)
        else:
            self.log.error("Failed to send message to topic %s", topic)

    #def _publish_brightness(self, topic, my_config):
    #    """
    #    publish the brightness topic
    #    """
    #    if BACKLIGHT is False:
    #        # feature is switched off
    #        return
    #    # call command to read the brightness
    #    err, msg = subprocess.getstatusoutput(
    #        my_config["get"].format(displayID=self.display_id)
    #    )
    #    if not err:
    #        bmin = my_config["min"]
    #        bmax = my_config["max"]
    #        msg = int(float(msg) * (100 / (bmax - bmin)))
    #        # send message to broker
    #        if self.brightness != msg or self.unpublished is True:
    #            result = self.client.publish(topic, msg)
    #            # result: [0, 1]
    #            status = result[0]
    #            if status == 0:
    #                self.log.debug("Send '%s' to topic %s", msg, topic)
    #                self.brightness = msg
    #            else:
    #                self.log.error("Failed to send message to topic %s", topic)
    #    else:
    #        self.log.error("Error reading display brightness: %s", err)

    def _publish_shell_cmd(self, topic, my_config): # pylint: disable=unused-argument
        """
        publish the shell command topic
        """
        if self.shell_cmd != self.published_shell_cmd or self.unpublished is True:
            result = self.client.publish(topic, self.shell_cmd.capitalize())
            # result: [0, 1]
            status = result[0]
            if status == 0:
                self.log.debug("Send '%s' to topic %s", self.shell_cmd, topic)
                self.published_shell_cmd = self.shell_cmd
            else:
                self.log.error("Failed to send message to topic %s", topic)

    def _publish_backlight(self, topic, my_config, force_value=None):
        """
        publish the backlight topic. 
        If force_value is provided ('ON' or 'OFF'), it skips hardware reading.
        """
        if BACKLIGHT is False:
            return

        value = None

        if force_value is not None:
            # Use the forced value (e.g., from a shell command)
            value = force_value.upper()
        #else:
            # Standard logic: call command to read the hardware state
            #err, msg = subprocess.getstatusoutput(
            #    my_config["get"].format(displayID=self.display_id)
            #)
            #if not err:
            #    on_val = my_config["ON"]
            #    msg = msg.strip()
            #    value = "ON" if msg == on_val else "OFF"
            #else:
            #    self.log.error("Error reading display backlight status: %s", err)
            #    return
        if value is None or value == "":
            #self.log.warning("Attempted to publish empty backlight state. Aborting.")
            return
        # send message to broker if state changed or first-time publish
        if self.backlight_published != value or self.unpublished is True:
            result = self.client.publish(topic, value)
            if result[0] == 0:
                self.log.debug("Send '%s' to topic %s", value, topic)
                self.backlight = value
                self.backlight_published = value
            else:
                self.log.error("Failed to send message to topic %s", topic)

    def _publish_url(self, topic, my_config): # pylint: disable=unused-argument
        """
        publish the url topic
        """
        #Get current url from chrome:
        current_url = self.chrome_pages.active_url()
        if self.published_url != current_url or self.unpublished is True:
            result = self.client.publish(topic, current_url)
            # result: [0, 1]
            status = result[0]
            if status == 0:
                self.log.debug("Send '%s' to topic %s",current_url, topic)
                self.published_url = current_url
            else:
                self.log.error("Failed to send message to topic %s", topic)

    def _publish_panel(self, topic, my_config): # pylint: disable=unused-argument
        """
        publish the panel topic
        """
        #try to find panel by current url from chrome:
        current_url = self.chrome_pages.active_url()
        self.current_panel = PANEL_SHOW_URL
        self.autogui_commands = None
        #search in panel configuration
        for panel_name, url in my_config['panels'].items():
            #remove autogui commands in url definition:
            if len(url.split('|')) > 1:
                cmds = url.split('|')[1]
            else:
                cmds = None
            url = url.split('|')[0]
            if current_url == url:
                self.current_panel = panel_name
                self.autogui_commands = cmds
                break
        if ( self.current_panel != self.current_panel_published or
            self.unpublished is True ):
            result = self.client.publish(topic, self.current_panel.capitalize())
            # result: [0, 1]
            status = result[0]
            if status == 0:
                self.log.debug("Send '%s' to topic %s", self.current_panel, topic)
                self.current_panel_published = self.current_panel
            else:
                self.log.error("Failed to send message to topic %s", topic)

    def _publish_autogui_results(self, topic, my_config): # pylint: disable=unused-argument
        """
        publish the autogui result topic
        """
        # publish result of last autogui commads
        if PYAUTOGUI is True:
            if (
                self.unpublished is True
                or self.autogui_feedback != self.autogui_feedback_published
            ):
                result = self.client.publish(topic, self.autogui_feedback)
                # result: [0, 1]
                status = result[0]
                if status == 0:
                    self.log.debug("Send '%s' to topic %s", self.autogui_feedback, topic)
                    self.autogui_feedback_published = self.autogui_feedback
                else:
                    self.log.error("Failed to send message to topic %s", topic)

    def ha_discover(self):
        """
        piblish all ropics needed for the home assistant mqtt discovery
        """
        # cpu temperature
        topic, payload = self.ha.sensor(
            "cpu temperature",
            self.topic_root + "/system",
            "cpu_temp",
            "temperature",
            "°C",
            icon="cpu-64-bit"
        )
        self.ha_publish(topic, payload)

        if LD2450:
            topic, payload = self.ha.sensor(
                "Radar Occupancy", 
                self.topic_root + "/ld2450", 
                "target_count", 
                icon="radar"
            )
            self.ha_publish(topic, payload)

        # chrome tabs
        topic, payload = self.ha.sensor("Active chrome tabs",
                                   self.topic_root + "/system",
                                   "chrome_tabs",
                                   icon="monitor-dashboard"
                                   )
        self.ha_publish(topic, payload)

        # cpu load
        topic, payload = self.ha.sensor("cpu load",
                                   self.topic_root + "/system",
                                   "cpu_load",
                                   icon="cpu-64-bit"
                                   )
        self.ha_publish(topic, payload)

        # disk usage
        topic, payload = self.ha.sensor(
            "disk usage", self.topic_root + "/system", "disk_usage", icon="harddisk", unit="%"
        )
        self.ha_publish(topic, payload)

        # url
        topic, payload = self.ha.text("URL", self.topic_root + "/url")
        self.ha_publish(topic, payload)

        # panel select
        options = list(self.topic_config["panel"]["panels"])
        for i, option in enumerate(options):
            options[i] = option.capitalize()
        topic, payload = self.ha.select("Panel", self.topic_root + "/panel", options)
        self.ha_publish(topic, payload)

        # shell commands
        options = [IDLE] + list(self.topic_config["shell"]["commands"].keys())
        for i, option in enumerate(options):
            options[i] = option.capitalize()
        topic, payload = self.ha.select("shell command", self.topic_root + "/shell", options)
        self.ha_publish(topic, payload)

        #if BACKLIGHT is True:
        #    # backlight "light"
        #    topic, payload = self.ha.light(
        #        "backlight",
        #        self.topic_root + "/backlight",
        #        #self.topic_root + "/brightness_percent",
        #    )
        #    self.ha_publish(topic, payload)

        if PYAUTOGUI is True:
            # mouse x position
            topic, payload = self.ha.sensor(
                "Mouse X Pos", self.topic_root + "/system", "mouse_position[0]", icon="mouse"
            )
            self.ha_publish(topic, payload)
            # mouse y position
            topic, payload = self.ha.sensor(
                "Mouse Y Pos", self.topic_root + "/system", "mouse_position[1]", icon="mouse"
            )
            self.ha_publish(topic, payload)
            # autogui command string
            topic, payload = self.ha.text("AutoGUI command", self.topic_root + "/autogui")
            self.ha_publish(topic, payload)

    def publish_loop_callback(self):
        """
        add this to endless main publish loop
        """
        # call time time tick of chrome pages
        self.chrome_pages.tick()

def display_client():
    """
    main function to start the client
    """
    client = MqttDisplayClient(CONFIG_FILE)
    # connect() now handles the internal setup and the ONLINE publish
    client.connect()   
    client.init_i2c_sensors() 
    client.ha_discover()
    client.publish_loop()
    return client

def signal_term_handler( sig, frame ): # pylint: disable=unused-argument
    """
    Call back to handle OS SIGTERM signal to terminate client.
    """
    CLIENT.log.warning( "Received SIGTERM. Stop client...") # pylint: disable=possibly-used-before-assignment
    sys.exit(0)

if __name__ == "__main__":
    CLIENT = display_client()
    signal.signal(signal.SIGTERM, signal_term_handler )
