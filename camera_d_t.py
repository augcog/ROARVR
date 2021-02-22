import logging
import cv2
from cv2 import aruco
import pyrealsense2 as rs
import math as m
import numpy as np
import sys
import time

CAM_CONFIG = {
    "image_w": 848,
    "image_h": 480,
    "framerate": 30,
    "aruco_dict": aruco.DICT_5X5_250,
    "aruco_thres": 10,
    "aruco_block_size": 0.1592,
    "use_default_cam2cam": True,
    "detect_mode": False,
    # calibrate threshold in degree
    "calibrate_threshold": 1
}

class RS_D_T(object):

    def __init__(self, image_w=CAM_CONFIG["image_w"], image_h=CAM_CONFIG["image_h"], framerate=CAM_CONFIG["framerate"], GUI=True) -> None:
        self.logger = logging.getLogger("Intel RealSense D435i")
        self.logger.debug("Initiating Intel Realsense")

        self.show_gui = GUI
        self.font = cv2.FONT_HERSHEY_SIMPLEX
        self.line_type = cv2.LINE_AA
        # Declare RealSense pipeline, encapsulating the actual device and sensors
        self.pipe_d = rs.pipeline()
        cfg_d = rs.config()
        cfg_d.enable_stream(rs.stream.color, image_w, image_h, rs.format.bgr8, framerate)  # color camera

        # init for the t camera
        self.pipe_t = rs.pipeline()
        cfg_t = rs.config()
        cfg_t.enable_stream(rs.stream.pose)
        self.location: np.ndarray = np.array([0, 0, 0])  # x y z
        self.rotation: np.ndarray = np.array([0, 0, 0])  # pitch yaw roll
        # Start streaming with requested config
        self.prof_d, self.prof_t = None, None
        try:
            self.prof_d = self.pipe_d.start(cfg_d)
            self.prof_t = self.pipe_t.start(cfg_t)
        except Exception as e:
            self.stop()
            raise ConnectionError(f"Error {e}. Pipeline Initialization Error")
 
        self.running = True
        self.calibrated = False

        camera_intr = self.prof_d.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
        self.rgb_mtx = np.array([[camera_intr.fx, 0, camera_intr.ppx], [0, camera_intr.fy, camera_intr.ppy], [0, 0, 1]])
        self.rgb_dist = np.array(camera_intr.coeffs)

        # detection related params
        self.aruco_dict = aruco.Dictionary_get(CAM_CONFIG['aruco_dict'])
        self.parameters = aruco.DetectorParameters_create()
        self.parameters.adaptiveThreshConstant = CAM_CONFIG['aruco_thres']
        self.block_size = CAM_CONFIG['aruco_block_size']
        self.use_default_cam2cam = CAM_CONFIG['use_default_cam2cam']
        self.detect_mode = CAM_CONFIG['detect_mode']
        self.calibrate_thres = CAM_CONFIG['calibrate_threshold']

        self.t2d, self.d2m, self.t2m = None, None, None

        self.logger.info("Camera Initiated")

    """
    Important function that perform calibration check before start detecting ARUCO marker and getting
    transformation matrix
    """
    def calibrate(self, show_img=False):
        self.logger.debug("Calibration process starts")

        while not self.calibrated:
            frame_t = self.pipe_t.wait_for_frames()
            frame_d = self.pipe_d.wait_for_frames()

            pose_frame = frame_t.get_pose_frame()
            color_frame = frame_d.get_color_frame()

            img = self.color_frame_data(color_frame)
            t_tvec, t_rvec = self.pose_frame_data(pose_frame)

            rpy = self.rvec_to_rpy(t_rvec)

            if show_img:
                t_flag = max(rpy[0], rpy[2]) > self.calibrate_thres
                if t_flag:
                    boo, bar = round(rpy[0], 3), round(rpy[2], 3)
                    t_msg = "roll: {}, yall: {} | expected: both < 1".format(boo, bar)
                else:
                    t_msg = "calibration angles are correct"

                d2m, tvec, rvec = self.get_trans_mat(img)

                d_flag = d2m is None
                if d_flag:
                    d_msg = "aruco marker not detected"
                else:
                    d_msg = "aruco marker detected"

                cv2.putText(img, t_msg, (0, 40), self.font, 1, (0,255,255), 2, self.line_type)
                cv2.putText(img, d_msg, (0, 64), self.font, 1, (255,255,0), 2, self.line_type)
                cv2.imshow("frame", img)
                cv2.waitKey(100)

                if not (t_flag or d_flag):
                    self.d2m = d2m
                    self.t2d = self.cam2cam(t_rvec, t_tvec)
                    self.t2m = self.d2m @ self.t2d 
                    self.calibrated = True
                    print("calibration success: matrices loaded")
            else:
                # sleep for the command line mode since 
                time.sleep(0.5)
                if max(rpy[0], rpy[2]) > self.calibrate_thres:
                    print("calibration fails:")
                    print("roll: {}, yall: {} | expected: both < 1".format(rpy[0], rpy[2]))
                    continue

                d2m, tvec, rvec = self.get_trans_mat(img)
                if d2m is None:
                    print("calibration fails: \naruco marker not detected")
                else:
                    self.d2m = d2m
                    self.t2d = self.cam2cam(t_rvec, t_tvec)
                    self.t2m = self.d2m @ self.t2d 
                    self.calibrated = True
                    print("calibration success: matrices loaded")


    def get_intrinsics(self):
        return {
            'mtx': self.rgb_mtx,
            'dist': self.rgb_dist
        }

    def stop(self):
        self.pipe_d.stop()
        self.pipe_t.stop()
        self.logger.debug("Shutting Down")

    def start_detect(self):
        self.detect_mode = True
    
    def stop_detect(self):
        self.detect_mode = False

    """
    returns the [x, y, z] global coordiante of the vehicle (camera) in the map
    default format is a 3x1 np array, return np.nan when the pipeline errors
    """
    def poll(self):
        if not self.calibrated:
            self.logger.error("Camera not calibrated yet")
            return np.nan

        try:
            frame_d = self.pipe_d.wait_for_frames()
            frame_t = self.pipe_t.wait_for_frames()

            pose_frame = frame_t.get_pose_frame()
            color_frame = frame_d.get_color_frame()

            img = self.color_frame_data(color_frame)
            t_tvec, t_rvec = self.pose_frame_data(pose_frame)

            self.location = (self.t2m @ t_tvec)[:3]

            if self.show_gui:
                if self.detect_mode:
                    c2m, tvec, _ = self.get_trans_mat(img)
                    if tvec is not None:
                        rounded = np.round((c2m @ [0,0,0,1])[:3], decimals=3)
                        cv2.putText(img, str(rounded), (0, 64), self.font, 1, (255,255,0), 2, self.line_type)  
                
                rounded = np.round(self.location, decimals=3)
                cv2.putText(img, str(rounded), (0, 40), self.font, 1, (0,255,255), 2, self.line_type)
                
                # cv2.putText(img, str(t_tvec), (0, 40), self.font, 1, (0,255,255), 2, self.line_type)
                # cv2.putText(img, str(self.rvec_to_rpy(t_rvec)), (0, 64), self.font, 1, (255,255,0), 2, self.line_type)
                cv2.imshow("frame", img)
                key = cv2.waitKey(100)
                key_ord = key & 0xFF
                if key_ord == ord('q') or key == 27:
                    cv2.destroyAllWindows()
                    sys.exit(0)
                elif key_ord == ord('d'):
                    self.detect_mode = not self.detect_mode

            return self.location

        except Exception as e:
            logging.error(e)
            return np.nan

    def color_frame_data(self, color_frame):
        return np.asanyarray(color_frame.get_data())
    
    def pose_frame_data(self, pose_frame):
        data = pose_frame.get_pose_data()
        t = data.translation
        t_tvec = np.array([t.x, t.y, t.z, 1])
        r = data.rotation
        t_rvec = np.array([r.x, r.y, r.z, r.w])
        return t_tvec, t_rvec

    """
    Helper function that turns a rotation vector into roll, pitch, and yaw angles
    """
    def rvec_to_rpy(self, rvec):
        x, y, z, w = tuple(rvec)
        pitch =  -m.asin(2.0 * (x*z - w*y)) * 180.0 / m.pi
        roll  =  m.atan2(2.0 * (w*x + y*z), w*w - x*x - y*y + z*z) * 180.0 / m.pi
        yaw   =  m.atan2(2.0 * (w*z + x*y), w*w + x*x - y*y - z*z) * 180.0 / m.pi
        return np.array([roll, pitch, yaw])

    """
    This is a transformation from the d-camera's coord system to the marker's (world) system
    rvec and tvec are derived from the black-box algorithm in cv2.aruco, they represent some 
    important quantities from marker to d-camera. Since we want to extract the reverse transformation,
    we invert the matrix at the end.
    """
    def cam2marker(self, rvec, tvec):
        rmat = cv2.Rodrigues(rvec)[0]
        trans_mat = np.identity(4)
        trans_mat[:3, :3] = rmat
        trans_mat[:3, 3] = tvec
        trans_mat = np.linalg.inv(trans_mat)
        return trans_mat

    """
    This is a transformation from the t-camera's coordinate system to d-camera's.
    ** Why do we need to do this since these cameras are installed together? ** 
    1) t's coordinate axes are aligned independent of its own physical rotation, while
    d's are dependent.
    2) there's still some minor translation between their coordinate systems, which will
    be implemented later :TODO @Star
    """
    def cam2cam(self, t_rvec, t_tvec):
        base_c2c = np.array([1,0,0,0,
                             0,-1,0,0,
                             0,0,-1,0,
                             0,0,0,1]).reshape((4, 4))
        # no tuning of the t camera rotation
        if self.use_default_cam2cam:
            return base_c2c
        else:
            trans_mat = np.zeros((4, 4))
            trans_mat[:3,:3] = cv2.Rodrigues(t_rvec[:3])[0]
            trans_mat[3,:] = t_tvec
            trans_mat = np.linalg.inv(trans_mat)
            return trans_mat @ base_c2c

    def get_trans_mat(self, img):
        corners, ids, _ = aruco.detectMarkers(img, self.aruco_dict, parameters=self.parameters)
        if ids: # there's at least one aruco marker in sight
            rvec, tvec ,_ = aruco.estimatePoseSingleMarkers(corners, self.block_size, self.rgb_mtx, self.rgb_dist)
            d2m = self.cam2marker(rvec, tvec)

            if self.show_gui:
                aruco.drawAxis(img, self.rgb_mtx, self.rgb_dist, rvec[0], tvec[0], 0.01)
                aruco.drawDetectedMarkers(img, corners)

            return d2m, tvec, rvec
        else:
            return None, None, None

if __name__ == '__main__':
    camera = RS_D_T()
    counter = 0
    camera.calibrate(show_img=True)
    while True:
        loc = camera.poll()
        if np.isnan(loc).all():
            camera.stop()
            break
        if counter % 20 == 0:
            print(loc)
        counter += 1