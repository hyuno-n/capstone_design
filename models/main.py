from ultralytics import YOLO
import numpy as np
import cv2
import os
import datetime
from tensorflow.keras.models import load_model
from collections import defaultdict, deque
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import boto3
import requests
import threading
from concurrent.futures import ThreadPoolExecutor

# 환경 변수 로드 및 전역 상수 설정
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'

# 스레드 관리와 감지 상태를 위한 전역 변수
camera_threads = {}
detection_status = {}
thread_lock = threading.Lock()

# 스레드 풀 생성
executor = ThreadPoolExecutor()

# 비디오 감지 관련 설정
bg_subtractor = cv2.createBackgroundSubtractorMOG2()
GREEN = (0, 255, 0)
WHITE = (255, 255, 255)
output_width, output_height = 1920, 1080
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
fps = 30
buffer_length, post_event_length = 10 * fps, 10 * fps  # 10초 버퍼와 10초 후 이벤트

# 모델 불러오기 (LSTM 모델과 YOLO 모델)
lstm_model = load_model('model/final_lstm_model.h5')
yolo_model = YOLO("model/yolo11s-pose.pt")
fire_detect_model = YOLO("model/yolo11n-fire.pt")
classes = ['Fall', 'Normal']
default_class = 'Normal'
default_keypoints = np.zeros((12, 2))

# AWS S3 설정 (S3 저장소 및 폴더명)
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION_NAME = os.getenv("AWS_REGION_NAME")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
S3_FOLDER_NAME = "saved_clips"

# 객체 추적 및 예측 상태 관리
track_history = defaultdict(list)
object_predictions = {}

# 클립 저장을 위한 출력 디렉터리 설정
output_dir = "saved_clips"
if not os.path.exists(output_dir):
    os.makedirs(output_dir)

class EventDetector:
    def __init__(self, output_dir, fourcc, fps, post_event_length, s3_bucket_name, s3_folder_name):
        self.output_dir = output_dir
        self.fourcc = fourcc
        self.fps = fps
        self.post_event_length = post_event_length
        self.s3_bucket_name = s3_bucket_name
        self.s3_folder_name = s3_folder_name

        self.fall_detection_count = {}  # 각 객체의 낙상 감지 횟수를 저장할 딕셔너리
        self.event_detected = False
        self.frames_after_event = 0
        self.pre_event_buffer = deque(maxlen=buffer_length)
        self.out = None
        self.frames_written = 0
        self.local_filepath = None
        self.s3_key = None
        self.executor = ThreadPoolExecutor()

    def create_s3(self):
        """S3 클라이언트 생성"""
        try:
            s3_client = boto3.client(
                service_name='s3',
                aws_access_key_id=AWS_ACCESS_KEY_ID,
                aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                region_name=AWS_REGION_NAME
            )
            print("S3 클라이언트 생성 성공")
            return s3_client
        except Exception as e:
            print(f"S3 클라이언트 생성 중 오류 발생: {e}")
            return None
        
    def upload_to_s3(self, local_filepath, s3_bucket_name, s3_key):
        """로컬 파일을 S3에 업로드"""
        try:
            s3_client = self.create_s3()
            if s3_client:
                s3_client.upload_file(local_filepath, s3_bucket_name, s3_key)
                print(f"S3에 {local_filepath} 업로드 성공: {s3_key}")
            else:
                print("S3 클라이언트를 생성하지 못했습니다.")
        except Exception as e:
            print(f"S3 업로드 중 오류 발생: {e}")

    def send_alert(self, user_id, camera_number, event_name, timestamp):
        """이벤트 발생 시 알림을 보내는 함수"""
        print(f"경고: {event_name} 발생! 알림 전송 중...")

        url = f"http://{os.getenv('FLASK_APP_IP', '127.0.0.1')}:{os.getenv('FLASK_APP_PORT', '5000')}/log_event"

        payload = {
            'user_id': user_id,
            'timestamp': timestamp,
            'eventname': event_name,
            'camera_number': camera_number,
            'eventurl': ""
        }

        try:
            response = requests.post(url, headers={'Content-Type': 'application/json'}, json=payload)
            if response.status_code == 200:
                print("서버에 신호 전송 완료.")
            else:
                print("서버 신호 전송 실패:", response.status_code)
        except Exception as e:
            print("오류 발생:", e)

    def handle_event_detection(self, frame, predicted_label, user_id, camera_number):
        """이벤트 발생 감지 후 처리"""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if not self.event_detected or (self.frames_after_event > (self.post_event_length * 2)):
            if predicted_label in ['Fall', 'Movement', 'Black_smoke', 'Gray_smoke', 'White_smoke', 'Fire']:
                self.event_detected = True
                self.frames_after_event = 0
                print(f"{predicted_label} detected!")
                self.send_alert(user_id, camera_number, predicted_label, timestamp)

        if self.event_detected:
            self.save_event_clip(predicted_label, frame, timestamp)

            if self.frames_after_event >= self.post_event_length:
                self.event_detected = False

        self.frames_after_event += 1  # Increment frames after event

    def save_event_clip(self, event_name, frame, timestamp):
        """이벤트가 발생하면 영상을 저장하는 함수"""
        if self.out is None:
            clip_filename = f"{event_name}_{timestamp}.mp4"
            self.local_filepath = os.path.join(self.output_dir, clip_filename)
            self.s3_key = f"{self.s3_folder_name}/{clip_filename}"
            try:
                self.out = cv2.VideoWriter(self.local_filepath, self.fourcc, self.fps, (output_width, output_height))
                print(f"비디오 라이터 초기화 완료: {clip_filename}")
            except Exception as e:
                print(f"비디오 라이터 초기화 중 오류 발생: {e}")
                return
            buffer_size = len(self.pre_event_buffer)
            if buffer_size > 0:
                print(f"이벤트 발생 전 {buffer_size}개의 프레임을 저장합니다.")
                for buffered_frame in self.pre_event_buffer:
                    self.out.write(buffered_frame)
            else:
                print("이벤트 발생 전 저장할 프레임이 충분하지 않습니다.")

        try:
            self.out.write(frame)
            self.frames_written += 1
        except Exception as e:
            print(f"프레임 쓰기 중 오류 발생: {e}")

        # 이벤트 후 클립 저장이 완료되면 S3에 업로드
        if self.frames_written >= self.post_event_length:
            self.out.release()
            self.out = None
            print("이벤트 클립 저장 완료.")
            
            # 클립 파일을 S3에 업로드
            future = self.executor.submit(self.upload_to_s3, self.local_filepath, self.s3_bucket_name, self.s3_key)  # S3 업로드를 백그라운드 스레드로 수행
            try:
                future.result()  # 업로드 완료를 대기
            except Exception as e:
                print(f"S3 업로드 중 오류 발생: {e}")

            # 로컬 파일 삭제
            if os.path.exists(self.local_filepath):
                os.remove(self.local_filepath)
                print(f"로컬 파일 삭제: {self.local_filepath}")

def preprocess_keypoints(keypoints):
    """키포인트 전처리"""
    if keypoints.shape[0] == 0:
        return default_keypoints

    body_keypoints_indices = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
    filtered_keypoints = keypoints[body_keypoints_indices, :2]
    return filtered_keypoints

def detect_people_and_keypoints(frame):
    """주어진 프레임에서 사람 및 키포인트 탐지"""
    track_ids, keypoints_list = [], []
    
    try:
        results = yolo_model.track(frame, persist=True, verbose=False)
        keypoints = results[0].keypoints
        boxes = results[0].boxes.xyxy.cpu().numpy()
        if results[0].boxes.id is not None:
            track_ids = results[0].boxes.id.int().cpu().tolist()
        else:
            track_ids = []  # None일 경우 기본값으로 빈 리스트 할당
        
        update_track_history(boxes, track_ids)
        if keypoints is not None:
            for kp in keypoints:
                keypoints_list.append(kp.xy[0].cpu().numpy())
    except AttributeError as e:
        print(e)
    
    return keypoints_list, boxes, track_ids

def detect_movement(frame, min_contour_area=10000):
    """영상처리를 이용한 움직임 감지"""
    fg_mask = bg_subtractor.apply(frame)
    fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, None, iterations=2)
    fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, None, iterations=2)
    contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    motion_detected = False
    largest_contour = None
    largest_area = 0

    # 모든 컨투어를 순회하며 가장 큰 컨투어를 찾기
    for contour in contours:
        area = cv2.contourArea(contour)
        if area > min_contour_area and area > largest_area:
            largest_area = area
            largest_contour = contour

    # 가장 큰 컨투어가 있을 경우 박스를 그리기
    if largest_contour is not None:
        x, y, w, h = cv2.boundingRect(largest_contour)
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
        motion_detected = True

    return frame, motion_detected

def update_track_history(boxes, track_ids):
    """경계 상자와 트랙 ID를 사용하여 추적 이력 업데이트"""
    for box, track_id in zip(boxes, track_ids):
        x, y, _, _ = box
        track_history[track_id].append((float(x), float(y)))
        if len(track_history[track_id]) > 30:
            track_history[track_id].pop(0)

def draw_skeletons_and_boxes(frame, keypoint, box):
    """프레임에 스켈레톤과 경계 상자 그리기"""
    if box is not None:
        x1, y1, x2, y2 = map(int, box)
        cv2.rectangle(frame, (x1, y1), (x2, y2), GREEN, 2)

    for (x, y) in keypoint:
        cv2.circle(frame, (int(x), int(y)), 3, GREEN, -1)
    
    return frame

def draw_detection_area(frame, roi_x1, roi_y1, roi_x2, roi_y2):
    """탐지할 영역(ROI)을 프레임에 그리는 함수"""
    cv2.rectangle(frame, (roi_x1, roi_y1), (roi_x2, roi_y2), (0, 0, 255), 2)  # 빨간색 사각형 그리기

def is_in_detection_area(x, y, roi_x1, roi_y1, roi_x2, roi_y2):
    """좌표가 탐지 범위(ROI) 내에 있는지 확인"""
    return (roi_x1 <= x <= roi_x2) and (roi_y1 <= y <= roi_y2)
    
def process_video(user_id, camera_id, rtsp_url):
    """비디오 프로세싱 메인 루프"""

    print(f"Thread started for camera {camera_id}.")
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        print(f"Unable to open camera {camera_id}.")
        return
    
    # 이벤트 감지 객체 생성
    event_detector = EventDetector(output_dir, fourcc, fps, post_event_length, S3_BUCKET_NAME, S3_FOLDER_NAME)
    
    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            print(f"Camera {camera_id} stream ended.")
            break
        camera_settings = detection_status[user_id]['camera_info'][camera_id]
    
        # 카메라 설정 로드
        roi_apply_signal = camera_settings.get('roi_detection_on', False)
        roi_x1 = camera_settings['roi_values'].get('roi_x1', 0)
        roi_y1 = camera_settings['roi_values'].get('roi_y1', 0)
        roi_x2 = camera_settings['roi_values'].get('roi_x2', 1920)
        roi_y2 = camera_settings['roi_values'].get('roi_y2', 1080)
        
        frame = cv2.resize(frame, (output_width, output_height), interpolation=cv2.INTER_CUBIC)

        if roi_apply_signal:
            draw_detection_area(frame, roi_x1, roi_y1, roi_x2, roi_y2)

        # 넘어짐 감지
        if camera_settings['fall_detection_on']:
            keypoints_list, boxes, track_ids = detect_people_and_keypoints(frame)
            detected_in_roi = []  # 여러 객체가 ROI 내에서 감지되었는지 확인하기 위한 리스트
            predictions = {}  # 각 객체의 예측을 저장할 딕셔너리
            
            for keypoints, track_id in zip(keypoints_list, track_ids):
                predicted_label = default_class
                # 키포인트가 기본값일 때의 처리 (예: 이벤트 감지 건너뛰기)
                if np.all(keypoints == (0, 0)):  # 기본값일 경우
                    continue  # 또는 적절한 다른 처리를 수행

                preprocessed_keypoints = preprocess_keypoints(keypoints)
                preprocessed_keypoints = preprocessed_keypoints.reshape(1, 12, 2)

                # LSTM 모델을 사용하여 예측
                predictions = lstm_model.predict(preprocessed_keypoints)

                predicted_class = np.argmax(predictions, axis=1)[0]
                predicted_label = classes[predicted_class]

                # 낙상 5회 이하 감지시 Normal로 검출
                object_predictions[track_id] = default_class

                # 낙상 감지 이벤트 카운트
                if predicted_label == 'Fall':
                    if track_id not in event_detector.fall_detection_count:
                        event_detector.fall_detection_count[track_id] = 0  # 카운트 초기화
                    event_detector.fall_detection_count[track_id] += 1  # 카운트 증가
                    
                else:
                    # 낙상이 아닐 경우 카운트 초기화
                    if track_id in event_detector.fall_detection_count:
                        event_detector.fall_detection_count[track_id] = 0  # 또는 감소 로직을 원하면 조정 가능

                 # 낙상이 5회 이상 감지된 경우
                if event_detector.fall_detection_count.get(track_id, 0) >= 5:
                    object_predictions[track_id] = 'Fall'
                    print(f"Track ID {track_id} - Fall detected!")
                
                for (x, y) in keypoints:
                    if is_in_detection_area(x, y, roi_x1, roi_y1, roi_x2, roi_y2):  # ROI 내에 있는지 확인
                        detected_in_roi.append(track_id)  # ROI 내에서 감지된 track_id를 추가
                        break  # ROI 내에 있는 점이 있으면 감지 성공으로 처리
                    
            # ROI 내에서 감지된 객체에 대해 이벤트 감지 및 시각화
            for track_id in detected_in_roi:                
                # 해당 객체에 대한 박스 및 키포인트 그리기
                if track_id in track_ids:
                    index = track_ids.index(track_id)
                    box = boxes[index]  # 현재 track_id에 해당하는 경계 상자를 찾음
                    x1, y1, _, _ = map(int, box)  # 좌상단 좌표 사용
                    
                    # 라벨을 박스의 왼쪽 위에 표시
                    cv2.putText(frame, object_predictions[track_id], (x1, y1 - 10), 
                                cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2, cv2.LINE_AA)
                    
                    # 해당 객체의 키포인트 그리기
                    draw_skeletons_and_boxes(frame, keypoints_list[index], box)

                    # 이벤트 발생 처리 함수
                    event_detector.handle_event_detection(frame, object_predictions[track_id], user_id, camera_id)
            

            # 추적 경로 그리기
            for track_id, track in track_history.items():
                if track:
                    points = np.hstack(track).astype(np.int32).reshape((-1, 1, 2))
                    cv2.polylines(frame, [points], isClosed=False, color=WHITE, thickness=10)
                    
        # 움직임 감지
        if camera_settings['movement_detection_on']:
            frame, motion_detected = detect_movement(frame)
            if motion_detected:
                event_detector.handle_event_detection(frame, 'Movement', user_id, camera_id)

        # 화재 감지
        if camera_settings['fire_detection_on']:
            fire_predictions = fire_detect_model.predict(source=frame, stream=True)
            fire_detected_in_roi = False
            for box in fire_predictions.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                if is_in_detection_area(x1, y1, roi_x1, roi_y1, roi_x2, roi_y2):
                    fire_detected_in_roi = True
                    break

            if fire_detected_in_roi:
                label = f"{fire_detect_model.names[int(box.cls[0])]}: {box.conf[0]:.2f}"
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(frame, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                event_detector.handle_event_detection(frame, 'Fire detected', user_id, camera_id)

        # 프레임을 계속해서 버퍼에 저장
        event_detector.pre_event_buffer.append(frame)

        cv2.imshow(f'Video {camera_id}', frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

@app.route('/add_camera', methods=['POST'])
def add_camera():
    """카메라 추가시 스레드 시작"""
    data = request.json
    user_id = data.get('user_id')
    camera_id = int(data.get('camera_id'))

    # camera_info에서 해당 camera_id에 대한 설정 가져오기
    camera_info = data.get('camera_info', {})
    camera_settings = camera_info.get(str(camera_id), {})

    # camera_settings에서 rtsp_url 가져오기
    rtsp_url = camera_settings.get('rtsp_url')
    # 유저별 카메라 정보를 저장하기 위해 detection_status 딕셔너리에서 user_id 확인
    if user_id not in detection_status:
        detection_status[user_id] = {'camera_info': {}}

    # 특정 카메라 ID가 이미 존재하는지 확인
    if camera_id in detection_status[user_id]['camera_info']:
        return jsonify({"message": f"Camera {camera_id} already exists for user {user_id}."}), 400

    # detection_status에 해당 카메라 설정 저장
    detection_status[user_id]['camera_info'][camera_id] = {
        'rtsp_url': rtsp_url,
        'fall_detection_on': camera_settings.get('fall_detection_on', False),
        'fire_detection_on': camera_settings.get('fire_detection_on', False),
        'movement_detection_on': camera_settings.get('movement_detection_on', False),
        'roi_detection_on': camera_settings.get('roi_detection_on', False),
        'roi_values': camera_settings.get('roi_values', {})
    }
    print(f"Adding camera: {camera_id} with RTSP: {rtsp_url}")

    # 새로운 카메라에 대해 스레드 시작
    if user_id not in camera_threads:
        camera_threads[user_id] = {}  # 새로운 사용자 ID 생성

    # 카메라 ID별로 쓰레드를 시작하여 사용자별로 저장
    thread = threading.Thread(target=process_video, args=(user_id, camera_id, rtsp_url))
    camera_threads[user_id][camera_id] = thread  # 사용자 ID별 카메라 ID에 쓰레드 저장
    thread.start()
    
    return jsonify({"message": "Camera added successfully."}), 200

@app.route('/event_update', methods=['POST'])
def event_update():
    """서버에서 탐지 기능 상태 가져오기"""
    try:
        # request.get_json() 사용하여 POST 요청의 JSON 데이터 가져오기
        data = request.get_json()
        if data is None:
            return jsonify({"error": "No JSON received"}), 400
        
        # user_id와 camera_info 가져오기
        user_id = data.get('user_id')
        if not user_id:
            return jsonify({"error": "user_id is required"}), 400
        
        camera_id = data.get('camera_id')  # 수정할 카메라 ID 추가
        if not camera_id:
            return jsonify({"error": "camera_id is required"}), 400
        
        camera_info = data.get('camera_info', {})
        camera_data = camera_info.get(str(camera_id))  # camera_id에 해당하는 카메라 설정 가져오기
        if not camera_data:
            return jsonify({"error": f"camera_info for camera_id {camera_id} is required"}), 400
        
        # 사용자가 없을 때 camera_info 초기화
        if user_id not in detection_status:
            detection_status[user_id] = {'camera_info': {}}

        # 특정 카메라에 대한 탐지 상태 업데이트
        detection_status[user_id]['camera_info'][camera_id] = {
            'rtsp_url': camera_data.get('rtsp_url'),
            'fall_detection_on': camera_data.get('fall_detection_on', False),
            'fire_detection_on': camera_data.get('fire_detection_on', False),
            'movement_detection_on': camera_data.get('movement_detection_on', False),
            'roi_detection_on': camera_data.get('roi_detection_on', False),
            'roi_values': camera_data.get('roi_values', {})
        }

        # 카메라에 대한 스레드 실행
        rtsp_url = camera_data.get('rtsp_url')
        if rtsp_url:  # RTSP URL이 존재할 경우에만 스레드 시작
            if camera_id not in camera_threads[user_id]:  # 스레드가 존재하지 않을 경우
                print(f"Starting thread for {camera_id} with RTSP URL: {rtsp_url}")
                thread = threading.Thread(target=process_video, args=(user_id, camera_id, rtsp_url))
                camera_threads[user_id][camera_id] = thread  # 스레드를 딕셔너리에 추가
                thread.start()  # 스레드 시작

        # 상태 확인을 위한 로그 출력
        print(f"User ID: {user_id}, Camera ID: {camera_id}")
        print(f"RTSP URL: {detection_status[user_id]['camera_info'][camera_id]['rtsp_url']}")
        print(f"Fall detection: {detection_status[user_id]['camera_info'][camera_id]['fall_detection_on']}")
        print(f"Fire detection: {detection_status[user_id]['camera_info'][camera_id]['fire_detection_on']}")
        print(f"Movement detection: {detection_status[user_id]['camera_info'][camera_id]['movement_detection_on']}")
        print(f"ROI Detection: {detection_status[user_id]['camera_info'][camera_id]['roi_detection_on']}")
        print(f"ROI Values: {detection_status[user_id]['camera_info'][camera_id]['roi_values']}")

        return jsonify({"status": "Detection status updated", "user_id": user_id, "camera_id": camera_id}), 200
    
    except Exception as e:
        print(f"서버 통신 중 오류 발생: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/remove_camera', methods=['POST'])
def remove_camera():
    """카메라 삭제시 스레드 종료"""
    data = request.json
    user_id = data.get('user_id')
    camera_id = data.get('camera_id')

    # 사용자 ID가 detection_status에 존재하는지 확인
    if user_id not in detection_status:
        return jsonify({"message": f"User {user_id} not found."}), 404

    # 카메라 ID가 해당 사용자에 존재하는지 확인
    if camera_id not in detection_status[user_id]['camera_info']:
        return jsonify({"message": f"Camera {camera_id} not found for user {user_id}."}), 404

    # 카메라 정보 삭제
    del detection_status[user_id]['camera_info'][camera_id]
    print(f"Removing camera: {camera_id} for user: {user_id}")

    # 스레드 종료
    if camera_id in camera_threads[user_id]:
        camera_threads[user_id][camera_id].join()  # 해당 카메라의 스레드 종료
        del camera_threads[user_id][camera_id]  # 스레드 딕셔너리에서 제거

    return jsonify({"message": f"Camera {camera_id} removed successfully."}), 200

def main():
    # Flask 서버 실행
    app.run(host="0.0.0.0", port=8000, threaded=True, debug=True)

if __name__ == "__main__":
    main()
