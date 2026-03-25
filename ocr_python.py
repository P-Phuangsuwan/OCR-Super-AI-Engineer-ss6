import os
import torch
from PIL import Image
from transformers import AutoProcessor, AutoModelForCausalLM
import json
import csv
import re

def main():
    # 1. ตั้งค่าโฟลเดอร์รูปภาพของคุณ (ใน Windows ใช้ตัว r นำหน้าเพื่อป้องกันปัญหาเรื่อง / เครื่องหมายทับ)
    folder_path = r"C:\Users\Pc\OneDrive\Documents\New project\images" 
    
    # ตรวจสอบว่ามีโฟลเดอร์นี้ในคอมพิวเตอร์จริงหรือไม่
    if not os.path.exists(folder_path):
        print(f"❌ ไม่พบโฟลเดอร์: {folder_path}")
        return

    # กำหนดที่เซฟไฟล์ CSV ให้อยู่ในโฟลเดอร์เดียวกัน
    csv_filename = os.path.join(folder_path, "voters_results_local.csv")

    # 2. เช็คว่าคอมของคุณมีการ์ดจอ NVIDIA (CUDA) หรือไม่
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"🖥️ ระบบกำลังรันด้วย: {device.upper()}")
    if device == "cpu":
        print("คำเตือน: การรันด้วย CPU จะใช้เวลานานมากและกิน RAM เครื่องสูง")

    # 3. โหลดโมเดล
    print("\n⏳ กำลังโหลดโมเดล AI (Typhoon 1.5 Vision 8B)...")
    model_id = "scb10x/llama-3-typhoon-v1.5-8b-vision-preview"
    
    # ปรับชนิดตัวแปรเพื่อไม่ให้พังถ้ารันบนเครื่องที่ไม่มีจีพียู
    dtype = torch.float16 if device == "cuda" else torch.float32 
    
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=dtype,
        device_map="auto" if device == "cuda" else None
    )
    
    # ถ้ารันด้วย CPU ปกติเราต้องดันตัวโมเดลเข้า CPU เอง
    if device == "cpu":
        model.to(device)

    # 4. เตรียมคำสั่ง (Prompt) 
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image"},
                {"type": "text", "text": "ดึงข้อมูลไอดี (id) และ จำนวนโหวต (votes) จากรูปภาพนี้ ให้แปลงเป็นเลขอารบิก (0-9) ทั้งหมด \nจงตอบกลับในรูปแบบ JSON array อย่างเดียว เช่น [{\"id\": \"1234\", \"votes\": \"500\"}] โดยไม่ต้องมีคำอธิบายอื่นๆ นอกเหนือจาก JSON"}
            ]
        }
    ]
    prompt = processor.apply_chat_template(messages, add_generation_prompt=True)
    
    data_to_save = []
    
    print(f"\n📂 เริ่มต้นอ่านไฟล์จากโฟลเดอร์: {folder_path}")
    
    # 5. วนลูปอ่านรูปภาพทั้งหมดในโฟลเดอร์
    for filename in os.listdir(folder_path):
        if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
            image_path = os.path.join(folder_path, filename)
            print(f"กำลังดึงข้อมูลจากไฟล์: {filename}...")
            
            try:
                # โหลดรูปภาพ
                image = Image.open(image_path).convert("RGB")
                
                # นำภาพแพ็คใส่ Input Tensor
                inputs = processor(text=prompt, images=image, return_tensors="pt")
                
                # โยน Input เข้า GPU หรือ CPU ขึ้นอยู่กับเครื่อง
                inputs = {
                    k: v.to(device, dtype) if v.dtype == torch.float else v.to(device) 
                    for k, v in inputs.items()
                }
                
                # ประมวลผลจาก AI
                with torch.no_grad():
                    outputs = model.generate(**inputs, max_new_tokens=512, do_sample=False, temperature=0.0)
                
                # แกะคำตอบออก
                decoded_output = processor.decode(outputs[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
                
                # หาส่วนที่เป็น JSON array
                json_match = re.search(r'\[.*\]', decoded_output.replace('\n', ''), re.DOTALL)
                
                if json_match:
                    extracted_data = json.loads(json_match.group(0))
                    for item in extracted_data:
                        # กรองให้มั่นใจว่าเหลือแค่เลขอารบิก 0-9
                        clean_id = re.sub(r'[^0-9]', '', str(item.get('id', '')))
                        clean_votes = re.sub(r'[^0-9]', '', str(item.get('votes', '')))
                        
                        if clean_id or clean_votes:
                            data_to_save.append({
                                'filename': filename, 
                                'id': clean_id, 
                                'votes': clean_votes
                            })
                else:
                    print(f"   [!] ไม่พบโครงสร้าง JSON หรือ AI ตอบมาเป็นรูปแบบอื่น")
                    
            except Exception as e:
                print(f"   [!] ข้ามไฟล์นี้ เกิดข้อผิดพลาดในระบบ: {e}")

    # 6. บันทึกลงไฟล์ CSV ในโฟลเดอร์ของเครื่อง
    if data_to_save:
        try:
            with open(csv_filename, mode='w', newline='', encoding='utf-8-sig') as file:
                writer = csv.DictWriter(file, fieldnames=['filename', 'id', 'votes'])
                writer.writeheader()
                writer.writerows(data_to_save)
            print(f"\n✅ เสร็จสิ้น! บันทึกไฟล์ CSV เรียบร้อยแล้วที่:\n👉 {csv_filename}")
        except Exception as e:
            print(f"\n❌ บันทึก CSV ไม่สำเร็จ: {e}")
    else:
        print("\n❌ ไม่มีข้อมูลอะไรถูกอ่านมาได้เลย จึงไม่ได้สร้างไฟล์ CSV")

if __name__ == "__main__":
    main()
