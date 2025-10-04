# Sử dụng một image Python nhẹ làm nền tảng
FROM python:3.9-slim

# Thiết lập thư mục làm việc bên trong container
WORKDIR /app

# Sao chép file requirements.txt vào trước để tận dụng cache của Docker
COPY requirements.txt .

# Cài đặt các thư viện cần thiết
RUN pip install --no-cache-dir -r requirements.txt

# Sao chép file code của bạn vào
COPY ytchien.py .

# Lệnh sẽ được thực thi khi container khởi động
CMD ["python", "ytchien.py"]