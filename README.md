# Word Document Comparison Backend

Đây là dự án backend đầu tay của mình sau khi ra trường. Dự án được xây dựng với mục tiêu xử lý và so sánh hai tài liệu Word, giúp người dùng dễ dàng nhận biết các thay đổi giữa bản gốc và bản đã chỉnh sửa.

Khác với việc chỉ so sánh nội dung text đơn giản, backend của hệ thống tập trung vào việc trích xuất cấu trúc thật của tài liệu Word, bao gồm heading, đoạn văn, bảng, hình ảnh, shape và textbox. Sau đó hệ thống sẽ xử lý, căn chỉnh và phát hiện các thay đổi để trả về kết quả dạng JSON cho frontend hiển thị.

## Dự án làm gì?

Backend này nhận vào hai file Word:

* Tài liệu gốc
* Tài liệu đã chỉnh sửa

Sau khi nhận file, hệ thống sẽ phân tích nội dung của hai tài liệu, xác định các phần giống nhau và khác nhau, sau đó trả về kết quả so sánh để frontend có thể hiển thị theo dạng song song giữa bản gốc và bản sửa đổi.

Các thay đổi có thể bao gồm:

* Nội dung được thêm mới
* Nội dung bị xóa
* Nội dung bị chỉnh sửa
* Thay đổi trong bảng
* Thay đổi hình ảnh
* Thay đổi shape hoặc textbox
* Thay đổi cấu trúc tài liệu

## Chức năng chính

* Upload và xử lý hai tài liệu Word
* Hỗ trợ định dạng `.doc` và `.docx`
* Tự động chuyển đổi `.doc` sang `.docx` khi cần
* Trích xuất cấu trúc tài liệu thành dạng cây dữ liệu
* Xử lý các thành phần trong Word như:

  * Heading
  * Paragraph
  * Table
  * Nested table
  * Image
  * Shape
  * Textbox
* So sánh nội dung giữa hai tài liệu
* Phát hiện nội dung thêm, xóa, sửa
* Xử lý bảng có merge cell
* Xử lý bảng lồng nhau
* Xử lý hình ảnh inline và floating
* Trả kết quả dạng JSON cho frontend
* Hỗ trợ export kết quả so sánh ra file Word
* Xử lý tác vụ so sánh bằng cơ chế job bất đồng bộ

## Luồng xử lý backend

1. Người dùng upload hai file Word lên hệ thống
2. Backend nhận file và lưu vào thư mục xử lý
3. Nếu file là `.doc`, hệ thống chuyển đổi sang `.docx`
4. Backend trích xuất nội dung từng file thành cấu trúc dữ liệu
5. Các thành phần trong tài liệu được chuẩn hóa thành các block có thể so sánh
6. Hệ thống căn chỉnh các block giữa bản gốc và bản sửa đổi
7. Backend thực hiện so sánh text, bảng, hình ảnh và shape
8. Kết quả thay đổi được tổng hợp thành JSON
9. Frontend sử dụng JSON để hiển thị giao diện so sánh
10. Người dùng có thể export kết quả so sánh ra file Word check sheet

## Công nghệ sử dụng

* Python
* FastAPI
* SQLite
* Microsoft Word COM Automation
* python-docx
* pywin32
* JSON API
* Async job queue

## Cấu trúc xử lý chính

Backend được chia thành nhiều phần xử lý riêng biệt:

* Extractor: đọc và trích xuất nội dung từ file Word
* Block Builder: chuẩn hóa nội dung tài liệu thành các block để so sánh
* Align: căn chỉnh các block giữa hai tài liệu
* Diff: xử lý logic so sánh nội dung
* Table Diff: xử lý so sánh bảng, bao gồm bảng merge cell và nested table
* Page Service: lấy thông tin trang thật từ Microsoft Word
* Serializer: chuyển kết quả xử lý thành JSON cho frontend
* Export: sinh file Word kết quả so sánh

## Một số bài toán đã xử lý

Trong quá trình phát triển, dự án gặp nhiều trường hợp thực tế khá phức tạp của file Word, ví dụ:

* Bảng có merge cell
* Bảng dài nhiều trang
* Bảng lồng nhau
* Hình ảnh nằm trong đoạn văn
* Hình ảnh floating
* Shape và textbox
* Heading có đánh số tự động
* Nội dung có nhiều format khác nhau
* Căn chỉnh nội dung giữa hai tài liệu không hoàn toàn giống nhau
* Lấy số trang thật của paragraph, table và shape thông qua Word COM

Đây cũng là phần giúp mình học được nhiều nhất trong quá trình làm dự án, vì file Word không chỉ đơn giản là text mà còn có rất nhiều cấu trúc ẩn bên trong.

## Cài đặt thư viện

Cài đặt các thư viện cần thiết bằng lệnh:

```bash
pip install -r requirements.txt
```

## Chạy backend

Chạy server FastAPI:

```bash
uvicorn main:app --reload
```

Hoặc nếu entry point nằm trong thư mục `src`, có thể chạy theo cấu trúc thực tế của project:

```bash
uvicorn src.main:app --reload
```

## Lưu ý

Dự án có sử dụng Microsoft Word COM Automation, vì vậy backend cần chạy trên môi trường Windows có cài Microsoft Word. Một số chức năng như chuyển đổi `.doc` sang `.docx` và lấy thông tin trang thật của tài liệu phụ thuộc vào Word COM.

Trong quá trình xử lý file Word thực tế, một số trường hợp phức tạp có thể ảnh hưởng đến kết quả so sánh, đặc biệt là với bảng. Ví dụ, nếu nhiều bảng liên tiếp có cùng heading hoặc phần tiêu đề bảng giống nhau, hệ thống có thể nhận diện nhầm rằng các bảng đó thuộc cùng một nhóm và thực hiện merge bảng không đúng. Khi đó kết quả so sánh bảng có thể không chính xác hoặc không phát hiện được đầy đủ thay đổi.

Ngoài ra, với các bảng có cấu trúc quá phức tạp như nhiều merge cell, nested table nhiều cấp, hoặc layout bị thay đổi mạnh giữa hai phiên bản tài liệu, việc căn chỉnh hàng và ô có thể chưa hoàn toàn chính xác trong mọi trường hợp.


## Mục tiêu của dự án

Mục tiêu của dự án là xây dựng một backend có khả năng xử lý tài liệu Word thực tế, phát hiện thay đổi một cách rõ ràng và cung cấp dữ liệu đủ tốt để frontend có thể hiển thị kết quả so sánh trực quan.

Đây không chỉ là một sản phẩm thử nghiệm, mà còn là quá trình mình học cách thiết kế backend, tổ chức code, xử lý dữ liệu phức tạp, xây dựng API và giải quyết lỗi trong một bài toán thực tế.

