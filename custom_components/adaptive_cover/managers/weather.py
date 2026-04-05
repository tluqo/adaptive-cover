import time

class WeatherManager:
    def __init__(self):
        self.wind_speed = 0
        self.rain = False
        self.binary_sensor_state = False
        self.clear_delay = 0

    def update_weather(self, wind_speed, rain):
        self.wind_speed = wind_speed
        self.rain = rain
        self.update_binary_sensor()

    def update_binary_sensor(self):
        if self.rain:
            self.binary_sensor_state = True
            time.sleep(self.clear_delay)  # Simulating the clear-delay timeout
            self.binary_sensor_state = False
        else:
            self.binary_sensor_state = False

    def set_clear_delay(self, seconds):
        self.clear_delay = seconds
