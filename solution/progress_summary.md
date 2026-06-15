# Progress Summary

Lần chạy public đầu tiên điểm rất thấp, chỉ khoảng 50 điểm, vì model trả lời rỗng hoặc bịa tổng tiền.
Tôi đã giữ lại log `run_output.json` và telemetry để xem lỗi thật thay vì đoán.
Sau đó sửa `prompt.txt` để bắt buộc grounding bằng tool, tính tiền chính xác và chống prompt injection.
Tiếp theo chỉnh `config.json` để giảm temperature, bật retry, cache, loop guard và redaction PII.
Trong `wrapper.py`, mình thêm logging, retry, sanitize input, và tự tính lại tổng từ trace để chống số học sai.
Tôi cũng cập nhật `findings.json` theo đúng lỗi quan sát được từ public run.
Cuối cùng đạt 100/100
Mục tiêu cuối là tăng correctness, giảm lỗi tool, và không để note ẩn hay PII làm model lệch hướng.
