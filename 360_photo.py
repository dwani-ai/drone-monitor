from djitellopy import Tello
import argparse
from pathlib import Path
import time
import cv2


parser = argparse.ArgumentParser()
parser.add_argument(
    "--output-dir",
    default=".",
    help="Directory where the four captured photos will be saved.",
)
args = parser.parse_args()
output_dir = Path(args.output_dir)
output_dir.mkdir(parents=True, exist_ok=True)


def safe_drone_call(label, call):
    try:
        call()
    except Exception as exc:
        print(f"Warning: {label} failed: {exc}")


def take_photo(drone, angle_label):
    # Grab the current frame from the camera stream
    frame = drone.get_frame_read().frame
    # Convert color space (Tello streams in RGB, OpenCV saves in BGR)
    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    # Save the image file
    filename = output_dir / f"tello_photo_{angle_label}_deg.jpg"
    cv2.imwrite(str(filename), frame_bgr)
    print(f"Photo saved: {filename}")
    time.sleep(1) # Short pause to ensure stability


def main():
    drone = Tello()
    stream_started = False
    took_off = False

    try:
        # 1. Initialize and connect to the drone
        drone.connect()
        print(f"Battery level: {drone.get_battery()}%")

        # 2. Start the video stream and give it a moment to initialize
        drone.streamon()
        stream_started = True
        time.sleep(2)

        # 3. Takeoff
        drone.takeoff()
        took_off = True
        time.sleep(2) # Let the drone stabilize its hover

        # 4. Panoramic sequence (4 captures total)
        print("Starting 360-degree photo sequence...")
        take_photo(drone, "0")

        drone.rotate_clockwise(90)
        time.sleep(1) # Let the drone stop shaking
        take_photo(drone, "90")

        drone.rotate_clockwise(90)
        time.sleep(1)
        take_photo(drone, "180")

        drone.rotate_clockwise(90)
        time.sleep(1)
        take_photo(drone, "270")

        # Optional: rotate to face forward again. Do not fail the capture if this
        # cleanup rotation is rejected, for example due to low voltage auto-land.
        safe_drone_call("final rotate", lambda: drone.rotate_clockwise(90))
        time.sleep(1)
    finally:
        print("Sequence complete. Landing...")
        if took_off:
            safe_drone_call("land", drone.land)
        if stream_started:
            safe_drone_call("streamoff", drone.streamoff)
        safe_drone_call("end", drone.end)


if __name__ == "__main__":
    main()
