import numpy as np
import tensorflow as tf
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense, Dropout, Bidirectional, Conv1D, MaxPooling1D
from tensorflow.keras.regularizers import l2
from tensorflow.keras.callbacks import EarlyStopping, ReduceLROnPlateau
import matplotlib.pyplot as plt

# GPU 사용 여부 확인
physical_devices = tf.config.list_physical_devices('GPU')
if physical_devices:
    tf.config.experimental.set_memory_growth(physical_devices[0], True)

# 클래스 레이블 설정
classes = ['넘어짐', '정상']

# 넘파이 파일 로드
data = np.load('keypoints_data_1.npz')

train_data = data['train_data']
train_labels = data['train_labels']
valid_data = data['valid_data']
valid_labels = data['valid_labels']

# 데이터를 LSTM에 맞게 reshape (x, y 값만 사용)
num_keypoints = train_data.shape[1] // 3
train_data = train_data.reshape((train_data.shape[0], num_keypoints, 3))[:, :, :2]
valid_data = valid_data.reshape((valid_data.shape[0], num_keypoints, 3))[:, :, :2]

# LSTM 입력 크기 설정 (x, y 값만 사용하므로 2로 설정)
input_shape = (num_keypoints, 2)

# 모델 구축
model = Sequential()
model.add(Conv1D(64, kernel_size=3, activation='relu', input_shape=input_shape))
model.add(MaxPooling1D(pool_size=2))
model.add(Bidirectional(LSTM(128, return_sequences=True, kernel_regularizer=l2(0.002))))
model.add(Dropout(0.5))  # 드롭아웃 비율 증가
model.add(Bidirectional(LSTM(64, return_sequences=False, kernel_regularizer=l2(0.002))))
model.add(Dropout(0.5))  # 드롭아웃 비율 증가
model.add(Dense(128, activation='relu'))  # 추가 Dense 레이어
model.add(Dense(len(classes), activation='softmax'))  # 최종 클래스 수에 맞춰 조정

# 옵티마이저 설정
optimizer = tf.keras.optimizers.AdamW(learning_rate=0.0001)

# 모델 컴파일
model.compile(optimizer=optimizer, loss='sparse_categorical_crossentropy', metrics=['accuracy'])

# 모델 요약 출력
model.summary()

# EarlyStopping 콜백 설정
early_stopping = EarlyStopping(monitor='val_loss', patience=20, restore_best_weights=True)

# ReduceLROnPlateau 콜백 설정
reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.5, patience=5, min_lr=0.00001)

# 모델 학습
history = model.fit(train_data, train_labels, epochs=100, validation_data=(valid_data, valid_labels), callbacks=[early_stopping, reduce_lr])

# 학습 손실과 정확도 시각화
plt.figure(figsize=(12, 5))

# 손실 시각화
plt.subplot(1, 2, 1)
plt.plot(history.history['loss'], label='Train Loss')
plt.plot(history.history['val_loss'], label='Validation Loss')
plt.xlabel('Epoch')
plt.ylabel('Loss')
plt.legend()
plt.title('Loss over Epochs')

# 정확도 시각화
plt.subplot(1, 2, 2)
plt.plot(history.history['accuracy'], label='Train Accuracy')
plt.plot(history.history['val_accuracy'], label='Validation Accuracy')
plt.xlabel('Epoch')
plt.ylabel('Accuracy')
plt.legend()
plt.title('Accuracy over Epochs')

# 이미지 저장
plt.savefig('lstm_11633_history.png')

# 이미지 표시
plt.show()

# 모델 저장
model.save('lstm_model_11633.h5')
