from djitellopy import Tello
import time

# 1. Initialize the drone
drone = Tello()

# 2. Connect to the Tello
drone.connect()

# 3. Check battery (good to do before flying!)
print(f"Battery: {drone.get_battery()}%")

# 4. Take off
drone.takeoff()

# 5. Fly in a square (distances in centimeters)
#drone.move_forward(100)
time.sleep(1)
#drone.move_right(100)
#time.sleep(1)
#drone.move_backward(100)
#time.sleep(1)
#drone.move_left(100)
#time.sleep(1)

# 6. Land
drone.land()

# 7. End the connection
drone.end()
