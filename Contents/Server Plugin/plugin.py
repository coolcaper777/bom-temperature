import indigo
import ftplib
import xml.etree.ElementTree as ET
from datetime import datetime, date
import time
import re
import threading
from xml.etree.ElementTree import tostring

class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.logger.info(f"Indigo API Version detected: {indigo.server.apiVersion}")
        try:
            api_version = float(indigo.server.apiVersion)
        except (TypeError, ValueError):
            api_version = 0.0
        if api_version <= 3:
            self.logger.error(f"API version too old! Requires greater than 3.0, got {indigo.server.apiVersion}")
        self.activeDevices = {}
        self.updateThread = None
        self.stopThread = False

    def startup(self):
        self.logger.info("BoM Temperature Plugin starting up")
        if "ftpServer" not in self.pluginPrefs:
            self.pluginPrefs["ftpServer"] = "ftp.bom.gov.au"
            self.logger.info("Initialized FTP Server to default: ftp.bom.gov.au")
        self.stopThread = False

    def shutdown(self):
        self.logger.info("BoM Temperature Plugin shutting down")
        self.stopThread = True
        if self.updateThread is not None:
            self.updateThread.join()
            self.updateThread = None

    def deviceStartComm(self, device):
        self.logger.info(f"Starting device: {device.name}")
        self.activeDevices[device.id] = device
        key_value_list = [
            {'key': 'state', 'value': "Preparing"},
            {'key': 'temperature', 'value': 0.0},
            {'key': 'temperatureDisplay', 'value': "0°C"},
            {'key': 'lastUpdated', 'value': "Not updated"},
            {'key': 'xmlIssueTime', 'value': "Unknown"},
            {'key': 'lastUpdateCheck', 'value': "Not checked"},
            {'key': 'forecastPeriodDate', 'value': "Unknown"},
            {'key': 'weatherCondition', 'value': "Unknown"}
        ]
        device.updateStatesOnServer(key_value_list)
        device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
        self.updateDevice(device)
        self.scheduleUpdates()

    def deviceStopComm(self, device):
        self.logger.info(f"Stopping device: {device.name}")
        if device.id in self.activeDevices:
            del self.activeDevices[device.id]

    def updateDevice(self, device):
        productId = device.pluginProps.get("productId", "")
        location = device.pluginProps.get("location", "")
        if not productId:
            self.logger.error(f"Product ID not set for device {device.name}")
            device.updateStatesOnServer([
                {'key': 'state', 'value': "Unavailable"},
                {'key': 'temperatureDisplay', 'value': "Unavailable"},
                {'key': 'weatherCondition', 'value': "Unknown"}
            ])
            return
        if not location:
            self.logger.error(f"Location not set for device {device.name}")
            device.updateStatesOnServer([
                {'key': 'state', 'value': "Unavailable"},
                {'key': 'temperatureDisplay', 'value': "Unavailable"},
                {'key': 'weatherCondition', 'value': "Unknown"}
            ])
            return

        if not re.match(r'^ID[D,N,Q,S,T,V,W]\d{5}$', productId):
            self.logger.error(f"Invalid Product ID format for device {device.name}: {productId}. Must be 3 uppercase letters (IDD, IDN, IDQ, IDS, IDT, IDV, IDW) followed by 5 digits (e.g., IDW14199).")
            device.updateStatesOnServer([
                {'key': 'state', 'value': "Unavailable"},
                {'key': 'temperatureDisplay', 'value': "Unavailable"},
                {'key': 'weatherCondition', 'value': "Unknown"}
            ])
            return

        try:
            device.updateStatesOnServer([
                {'key': 'state', 'value': "Preparing"},
                {'key': 'temperatureDisplay', 'value': "Preparing"}
            ])
            ftp = ftplib.FTP(self.pluginPrefs.get("ftpServer", "ftp.bom.gov.au"))
            ftp.login()
            ftp.cwd("/anon/gen/fwo/")
            
            filename = f"{productId}.xml"
            xml_data = []
            ftp.retrlines(f"RETR {filename}", xml_data.append)
            xml_string = "\n".join(xml_data)
            ftp.quit()

            self.logger.debug(f"Fetched XML for {productId} (first 500 chars): {xml_string[:500]}...")

            root = ET.fromstring(xml_string)

            issue_time_element = root.find(".//issue-time-local")
            xml_issue_time = issue_time_element.text if issue_time_element is not None else "Unknown"
            self.logger.debug(f"XML issue time: {xml_issue_time}")

            temperature = None
            weather_condition = None
            forecast_period_date = None
            current_date = date.today().strftime("%Y-%m-%d")
            self.logger.debug(f"Searching for forecast on {current_date} for location: {location}")

            for area in root.findall(".//area"):
                area_desc = area.get("description", "").lower()
                self.logger.debug(f"Checking area: {area_desc}")
                if area_desc == location.lower():
                    for forecast in area.findall("forecast-period"):
                        start_time = forecast.get("start-time-local", "")
                        forecast_date = start_time.split("T")[0] if "T" in start_time else "Unknown"
                        self.logger.debug(f"Forecast period date: {forecast_date}")
                        if forecast_date == current_date:
                            temp_element = forecast.find("element[@type='air_temperature_maximum']")
                            condition_element = forecast.find("text[@type='precis']")
                            self.logger.debug(f"Temp element: {temp_element}, Condition element: {condition_element}")
                            if temp_element is not None and temp_element.text:
                                temperature = float(temp_element.text)
                                weather_condition = condition_element.text if condition_element is not None and condition_element.text else "Unknown"
                                forecast_period_date = start_time
                                self.logger.debug(f"Found temperature: {temperature}°C, condition: {weather_condition}")
                                if weather_condition == "Unknown":
                                    forecast_xml = tostring(forecast, encoding='unicode')
                                    self.logger.debug(f"Missing precis text in forecast-period: {forecast_xml}")
                                break
                    if temperature is not None:
                        break

            if temperature is not None:
                formatted_temp = f"{temperature}°C"
                key_value_list = [
                    {'key': 'state', 'value': "Ready"},
                    {'key': 'temperature', 'value': temperature},
                    {'key': 'temperatureDisplay', 'value': formatted_temp},
                    {'key': 'lastUpdated', 'value': datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                    {'key': 'xmlIssueTime', 'value': xml_issue_time},
                    {'key': 'forecastPeriodDate', 'value': forecast_period_date},
                    {'key': 'weatherCondition', 'value': weather_condition}
                ]
                device.updateStatesOnServer(key_value_list)
                device.updateStateImageOnServer(indigo.kStateImageSel.TemperatureSensor)
                self.logger.info(f"Updated {device.name} with temperature {temperature}°C and condition {weather_condition} for location {location}")
            else:
                self.logger.error(f"No temperature data found for location {location} on {current_date} in Product ID {productId}")
                device.updateStatesOnServer([
                    {'key': 'state', 'value': "Unavailable"},
                    {'key': 'temperatureDisplay', 'value': "Unavailable"},
                    {'key': 'weatherCondition', 'value': "Unknown"}
                ])

        except Exception as e:
            self.logger.error(f"Error updating device {device.name}: {str(e)}")
            device.updateStatesOnServer([
                {'key': 'state', 'value': "Unavailable"},
                {'key': 'temperatureDisplay', 'value': "Unavailable"},
                {'key': 'weatherCondition', 'value': "Unknown"}
            ])

    def scheduleUpdates(self):
        if self.updateThread is None:
            self.updateThread = threading.Thread(target=self.updateLoop, name="BoM Temperature Update Thread")
            self.updateThread.daemon = True
            self.updateThread.start()
            self.logger.info("Started update thread for BoM Temperature")

    def updateLoop(self):
        while not self.stopThread:
            for deviceId, device in list(self.activeDevices.items()):
                device.updateStatesOnServer([
                    {'key': 'lastUpdateCheck', 'value': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                ])
                updateInterval = int(device.pluginProps.get("updateInterval", 60)) * 60
                self.updateDevice(device)
                sleep_duration = updateInterval
                while not self.stopThread and sleep_duration > 0:
                    time.sleep(min(1, sleep_duration))
                    sleep_duration -= 1

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errorsDict = indigo.Dict()
        productId = valuesDict.get("productId", "")
        location = valuesDict.get("location", "")
        if not productId:
            errorsDict["productId"] = "Product ID is required"
        elif not re.match(r'^ID[D,N,Q,S,T,V,W]\d{5}$', productId):
            errorsDict["productId"] = "Invalid format. Must be 3 uppercase letters (IDD, IDN, IDQ, IDS, IDT, IDV, IDW) followed by 5 digits (e.g., IDW14199)"
        if not location:
            errorsDict["location"] = "Location is required"
        if errorsDict:
            return False, valuesDict, errorsDict
        return True, valuesDict

    def update_data(self, action):
        deviceId = action.deviceId
        if deviceId in self.activeDevices:
            device = self.activeDevices[deviceId]
            self.logger.info(f"Manually updating device: {device.name}")
            self.updateDevice(device)
        else:
            self.logger.error(f"Device with ID {deviceId} not found for manual update")