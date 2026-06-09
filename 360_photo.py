from djitellopy import Tello
import time
import cv2

# 1. Initialize and connect to the drone
drone = Tello()
drone.connect()
print(f"Battery level: {drone.get_battery()}%")

# 2. Start the video stream and give it a moment to initialize
drone.streamon()
time.sleep(2)

# Helper function to capture and save a photo
def take_photo(angle_label):
    # Grab the current frame from the camera stream
    frame = drone.get_frame_read().frame
    # Convert color space (Tello streams in RGB, OpenCV saves in BGR)
    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    # Save the image file
    filename = f"tello_photo_{angle_label}_deg.jpg"
    cv2.imwrite(filename, frame_bgr)
    print(f"📷 Photo saved: {filename}")
    time.sleep(1) # Short pause to ensure stability

# 3. Takeoff
drone.takeoff()
time.sleep(2) # Let the drone stabilize its hover

# 4. Panoramic sequence (4 captures total)
print("Starting 360-degree photo sequence...")

# Capture 1: Starting position (0 degrees)
take_photo("0")

# Capture 2: Rotate 90 degrees clockwise
drone.rotate_clockwise(90)
time.sleep(1) # Let the drone stop shaking
take_photo("90")

# Capture 3: Rotate another 90 degrees (180 total)
drone.rotate_clockwise(90)
time.sleep(1)
take_photo("180")

# Capture 4: Rotate another 90 degrees (270 total)
drone.rotate_clockwise(90)
time.sleep(1)
take_photo("270")

# Optional: Rotate the final 90 degrees to face forward again
drone.rotate_clockwise(90)
time.sleep(1)

# 5. Land and safely close connections
print("Sequence complete. Landing...")
drone.land()
drone.streamoff()
drone.end()
