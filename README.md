# Typhoon OCR 1.5 to CSV on Google Colab

โปรเจกต์นี้ถูกปรับให้ใช้งานบน Google Colab เป็นหลัก โดยใช้ `Typhoon OCR 1.5` เพื่ออ่านภาพเอกสารและดึงข้อมูลออกมาเป็น CSV ตามคอลัมน์:

- `id`
- `id_doc`
- `row_num`
- `party_name`
- `vote`

คอลัมน์ `vote` จะถูกแปลงให้เหลือเฉพาะเลขอารบิกเท่านั้น เช่น `๔๗,๒๕๒` จะกลายเป็น `47252`

ไฟล์หลักที่ควรใช้คือ [ocr_typhoon_to_csv_colab.ipynb](./ocr_typhoon_to_csv_colab.ipynb)

## วิธีใช้บน Colab

1. อัปโหลดโฟลเดอร์ชุดข้อมูลไปไว้ใน Google Drive

ตัวอย่าง path ที่โน้ตบุ๊กตั้งค่าไว้ล่วงหน้า:

```text
/content/drive/MyDrive/super-ai-engineer-season-6-ocr-2569/data/images
```

2. เปิดไฟล์ `ocr_typhoon_to_csv_colab.ipynb` ด้วย Google Colab

3. รัน cell ติดตั้ง package

```python
!pip -q install typhoon-ocr
```

4. mount Google Drive และใส่ `TYPHOON_OCR_API_KEY`

5. แก้ค่าตัวแปร config ให้ตรงกับ path ของคุณ เช่น:

```python
INPUT_DIR = Path("/content/drive/MyDrive/super-ai-engineer-season-6-ocr-2569/data/images")
OUTPUT_DIR = Path("/content/drive/MyDrive/typhoon_ocr_output")
```

6. รัน cell OCR เพื่อให้ระบบอ่านทุกภาพและเขียนไฟล์ CSV

7. เปิดผลลัพธ์จากไฟล์:

```text
/content/drive/MyDrive/typhoon_ocr_output/ocr_results.csv
```

## โครงสร้างของโน้ตบุ๊ก

โน้ตบุ๊กถูกแบ่งเป็น 5 ส่วนหลัก:

1. ติดตั้ง dependency
2. mount Drive และตั้งค่า API key
3. ตั้งค่า path และ parameter ของงาน
4. นิยามฟังก์ชัน OCR + parser + CSV export
5. รัน pipeline และ preview ผลลัพธ์

## อธิบายโค้ดอย่างละเอียด

### 1. ติดตั้ง dependency

ใน Colab เราไม่ใช้ environment จากเครื่อง local เดิม จึงต้องติดตั้ง package ใหม่ใน runtime ทุกครั้งที่เปิด session ใหม่:

```python
!pip -q install typhoon-ocr
```

เหตุผลที่ใช้แค่ package นี้เพราะ Typhoon มี helper function `ocr_document()` ให้เรียก OCR ได้ตรง ๆ โดยไม่ต้องเขียน HTTP client เอง

### 2. mount Google Drive

เนื่องจากไฟล์ภาพต้นฉบับมาจากเครื่องของคุณ แต่ Colab รันอยู่บน cloud ดังนั้นต้องย้ายไฟล์ไป Google Drive ก่อน แล้ว mount เข้ามา:

```python
from google.colab import drive
drive.mount("/content/drive")
```

หลัง mount แล้ว Colab จะเห็นไฟล์ใน `MyDrive` เป็น path ปกติ และเราสามารถอ่านภาพหรือเขียน CSV กลับเข้า Drive ได้เลย

### 3. การตั้งค่า API key

Typhoon OCR ต้องใช้ API key จึงมี cell ที่รับค่าผ่าน `getpass()`:

```python
from getpass import getpass
os.environ["TYPHOON_OCR_API_KEY"] = getpass("Typhoon OCR API key: ")
```

ข้อดีคือเวลาพิมพ์ key ลงไป ค่าจะไม่แสดงบนหน้าจอ ลดโอกาสที่ secret จะหลุดในโน้ตบุ๊ก

### 4. การตั้งค่า path และ parameter

โน้ตบุ๊กมีตัวแปร config กลาง เช่น:

- `INPUT_DIR` โฟลเดอร์ภาพใน Drive
- `OUTPUT_DIR` โฟลเดอร์ปลายทาง
- `OUTPUT_CSV` ตำแหน่งไฟล์ CSV
- `CACHE_DIR` โฟลเดอร์เก็บผล OCR แบบ markdown
- `SLEEP_SECONDS` หน่วงเวลาเพื่อหลบ rate limit
- `RETRIES` จำนวนครั้งที่ retry
- `OVERWRITE_CACHE` บังคับ OCR ใหม่
- `MAX_FILES` จำกัดจำนวนไฟล์ตอนทดลอง

วิธีนี้ทำให้เวลาเปลี่ยน dataset หรือ output path ไม่ต้องแก้โค้ดหลายจุด

### 5. การแปลงเลขไทยเป็นเลขอารบิก

จุดสำคัญของโจทย์คือ `vote` ต้องเป็นเลขอารบิกเท่านั้น จึงมีสองฟังก์ชันหลัก:

```python
thai_to_arabic(text)
digits_only(text)
```

`thai_to_arabic()` ใช้ตารางแปลง:

- `๐ -> 0`
- `๑ -> 1`
- `๒ -> 2`
- `๓ -> 3`
- `๔ -> 4`
- `๕ -> 5`
- `๖ -> 6`
- `๗ -> 7`
- `๘ -> 8`
- `๙ -> 9`

จากนั้น `digits_only()` จะลบเครื่องหมาย comma, วงเล็บ และข้อความไทยออก เหลือเฉพาะตัวเลขจริง

ตัวอย่าง:

- `๓๙,๑๙๗` -> `39197`
- `๑๐,๐๓๒ (หนึ่งหมื่นสามสิบสอง)` -> `10032`

### 6. การสร้าง `id_doc`

ชื่อไฟล์ในชุดข้อมูลมีรูปแบบประมาณนี้:

- `constituency_10_1.png`
- `constituency_10_1_page2.png`
- `constituency_10_1_page3.png`

ฟังก์ชัน `derive_id_doc()` จะตัด `_page2`, `_page3` ออก เพื่อรวมหลายหน้าให้อยู่ในเอกสารเดียวกัน:

- `constituency_10_1.png` -> `constituency_10_1`
- `constituency_10_1_page2.png` -> `constituency_10_1`
- `constituency_10_1_page3.png` -> `constituency_10_1`

ผลคือข้อมูลทั้งหมดของเอกสารเดียวกันจะถูกรวมภายใต้ `id_doc` เดียว

### 7. การเรียก Typhoon OCR 1.5

ฟังก์ชัน `get_ocr_markdown()` เรียก OCR จริงด้วย:

```python
from typhoon_ocr import ocr_document
markdown = ocr_document(pdf_or_image_path=str(pdf_or_image_path))
```

Typhoon OCR จะคืนผลกลับมาเป็น markdown ซึ่งมีทั้งข้อความและโครงสร้างเอกสาร ทำให้ parser อ่านตารางต่อได้ง่ายกว่า OCR แบบ text ล้วน

ในฟังก์ชันนี้มีความสามารถเพิ่มอีก 3 อย่าง:

- cache ผล OCR เป็นไฟล์ `.md`
- retry เมื่อ request ล้มเหลว
- sleep ระหว่าง request เพื่อให้เหมาะกับ rate limit

### 8. การ parse ตาราง

ฟังก์ชัน `parse_markdown_table()` จะมองหาบรรทัดที่เป็นตาราง markdown โดยใช้ `|` เป็นตัวแบ่งคอลัมน์ แล้วดึงข้อมูลตาม schema:

- คอลัมน์แรก -> `row_num`
- คอลัมน์รองสุดท้าย -> `party_name`
- คอลัมน์สุดท้าย -> `vote`

พร้อมทั้งข้าม:

- บรรทัด header
- บรรทัด separator เช่น `|---|---|`
- บรรทัดที่ไม่มีเลขคะแนน

เหตุผลที่ใช้คอลัมน์รองสุดท้ายเป็น `party_name` เพราะเราต้องการพรรค ไม่ต้องการชื่อผู้สมัคร และวิธีนี้ยืดหยุ่นกับผล OCR ที่คอลัมน์กลางอาจเพี้ยนเล็กน้อย

### 9. fallback สำหรับเอกสารที่ OCR ออกมาไม่เป็นตารางชัด

บางครั้ง OCR อาจไม่ได้คืน markdown table ที่สวยมาก จึงมี `parse_plain_text()` เป็นแผนสำรอง โดยใช้การ split ด้วยช่องว่างยาว ๆ เพื่อเดา:

- ช่องแรกเป็น `row_num`
- ช่องก่อนสุดท้ายเป็น `party_name`
- ช่องสุดท้ายเป็น `vote`

วิธีนี้ไม่ได้แม่นเท่า table parser แต่ช่วยให้โน้ตบุ๊กทนกับเอกสารสแกนที่คุณภาพไม่สม่ำเสมอ

### 10. การเขียน CSV

ฟังก์ชัน `write_csv()` เขียนผลลัพธ์ออกเป็นไฟล์ CSV ด้วยคอลัมน์:

```python
["id", "id_doc", "row_num", "party_name", "vote"]
```

โดย:

- `id` เป็น running number เริ่มจาก 1
- `id_doc` มาจากชื่อเอกสาร
- `row_num` มาจากลำดับผู้สมัครในเอกสาร
- `party_name` มาจากพรรคการเมือง
- `vote` ถูก normalize ให้เหลือเลขอารบิกเท่านั้น

ใช้ encoding `utf-8-sig` เพื่อให้เปิดใน Excel ได้ง่ายขึ้น

### 11. ลำดับการทำงานของ pipeline

ฟังก์ชัน `run_pipeline()` ทำงานตามลำดับนี้:

1. อ่านรายชื่อไฟล์ทั้งหมดจาก `INPUT_DIR`
2. เรียงไฟล์ตามลำดับเอกสารและหน้า
3. OCR ทีละไฟล์
4. parse แต่ละไฟล์เป็นหลายแถวข้อมูล
5. รวมทุกแถวเข้าลิสต์เดียว
6. เขียน CSV ตอนท้าย

ข้อดีของโครงสร้างนี้คือ debug ง่าย เพราะแต่ละหน้าที่แยกชัด:

- OCR
- parse
- normalize
- export

## หมายเหตุสำคัญ

- เอกสารหลายไฟล์มีหน้าแรกเป็นหน้าประกาศ และหน้าสุดท้ายเป็นหน้าลายเซ็น จึงเป็นเรื่องปกติที่บางภาพจะได้ `0 rows`
- หน้าที่มักมีข้อมูลผู้สมัครและคะแนนจริงคือหน้าประเภท `page2`
- ถ้าต้องการทดลองเร็วขึ้น ให้ตั้ง `MAX_FILES = 5` ก่อน
- ถ้าต้องการ OCR ใหม่ทั้งหมด ให้ตั้ง `OVERWRITE_CACHE = True`

## ไฟล์ในโปรเจกต์

- [ocr_typhoon_to_csv_colab.ipynb](./ocr_typhoon_to_csv_colab.ipynb) โน้ตบุ๊กสำหรับรันบน Colab
- [ocr_typhoon_to_csv.py](./ocr_typhoon_to_csv.py) เวอร์ชันสคริปต์เดิมที่ใช้ logic เดียวกัน
- [requirements.txt](./requirements.txt) dependency พื้นฐาน

ถ้าต้องการ ผมช่วยต่อยอดให้โน้ตบุ๊กสรุปไฟล์ที่ OCR ไม่ผ่าน หรือแยกผลตาม `id_doc` แบบอัตโนมัติได้ต่อครับ
