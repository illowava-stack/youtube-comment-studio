import os
import json
import asyncio
import re
import uuid
from PIL import Image
from playwright.async_api import async_playwright
from google import genai
from google.genai import types

TARGET_RATIO_MIN = 2 / 5
TARGET_RATIO_MAX = 5 / 2
BATCH_SIZE = 100
MIN_COMMENT_LENGTH = 5

def pad_image_to_aspect_ratio(image_path):
    try:
        with Image.open(image_path) as img:
            img = img.convert("RGBA")
            w, h = img.size
            ratio = w / h
            
            bg_color = img.getpixel((5, 5)) 
            
            new_w, new_h = w, h
            needs_padding = False
            
            if ratio > TARGET_RATIO_MAX:
                new_h = int(w / TARGET_RATIO_MAX)
                needs_padding = True
            elif ratio < TARGET_RATIO_MIN:
                new_w = int(h * TARGET_RATIO_MIN)
                needs_padding = True
                
            if needs_padding:
                new_img = Image.new("RGBA", (new_w, new_h), bg_color)
                offset_x = (new_w - w) // 2
                offset_y = (new_h - h) // 2
                new_img.paste(img, (offset_x, offset_y), img)
                new_img.save(image_path, "PNG")
                return True, w, h, new_w, new_h
            return False, w, h, w, h
    except Exception as e:
        print(f"Padding error: {e}")
        return False, 0, 0, 0, 0

async def process_youtube_comments(youtube_url: str, api_key: str, output_dir: str, task_id: str, skip: int = 0, is_rescan: bool = False, exclude_ids: list = None):
    if exclude_ids is None:
        exclude_ids = []
    if not api_key:
        yield {"type": "error", "message": "API Key is missing. Please provide your Gemini API key."}
        return

    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        yield {"type": "error", "message": f"Failed to initialize Gemini Client: {e}"}
        return
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    yield {"type": "log", "message": f"Starting analysis for {youtube_url}...", "progress": 5}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1024, "height": 1080})
        await page.emulate_media(color_scheme="dark")
        
        yield {"type": "log", "message": "Loading YouTube page...", "progress": 10}
        await page.goto(youtube_url)
        # Handle YouTube consent popup if it appears
        try:
            consent_btn = page.locator("button", has_text=re.compile(r"Accept all|Reject all|모두 동의|모두 거절", re.IGNORECASE)).first
            if await consent_btn.is_visible(timeout=1000):
                await consent_btn.click()
                await asyncio.sleep(1)
        except:
            pass
        
        try:
            await page.wait_for_selector("video")
            await page.evaluate("document.querySelector('video').pause()")
        except:
            pass
            
        yield {"type": "log", "message": "Extracting video title...", "progress": 15}
        try:
            title_element = await page.wait_for_selector("h1.ytd-watch-metadata yt-formatted-string", timeout=15000)
            title_text = await title_element.inner_text()
            safe_title = re.sub(r'[\\/*?:"<>|]', "", title_text).strip()
        except:
            safe_title = f"Unknown_Video_{task_id}"
            
        yield {"type": "log", "message": f"Video title: {safe_title}. Scrolling to load comments...", "progress": 20}
        
        await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(1)
        await page.evaluate("window.scrollBy(0, 600)")
        
        try:
            await page.wait_for_selector("ytd-comments", timeout=15000)
        except:
            yield {"type": "error", "message": "Could not find the comments section."}
            await browser.close()
            return
            
        MAX_COMMENTS = 20
        target_count = skip + MAX_COMMENTS
        previous_count = 0
        unchanged_cycles = 0
        comment_elements = []
        
        while unchanged_cycles < 5 and len(comment_elements) < target_count:
            if comment_elements:
                try:
                    await comment_elements[-1].scroll_into_view_if_needed()
                except:
                    pass
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
            
            comment_elements = await page.locator("ytd-comment-thread-renderer").all()
            current_count = len(comment_elements)
            
            yield {"type": "log", "message": f"Loaded {current_count} comments (Target: {target_count})...", "progress": 20 + min((current_count/target_count)*20, 20)}
            
            if current_count >= target_count:
                comment_elements = comment_elements[:target_count]
                break
                
            if current_count > previous_count:
                unchanged_cycles = 0
                previous_count = current_count
            else:
                unchanged_cycles += 1
                
        # Slice to ignore the skipped comments
        comment_elements = comment_elements[skip:]
        
        yield {"type": "log", "message": f"Finished scrolling. Processing {len(comment_elements)} new comments. Expanding 'Read more' buttons...", "progress": 45}
        
        clicked = 0
        for el in comment_elements:
            try:
                btn = el.locator("tp-yt-paper-button#more").first
                if await btn.is_visible():
                    await btn.click(timeout=1000)
                    clicked += 1
            except:
                pass
        
        if clicked > 0:
            yield {"type": "log", "message": f"Expanded {clicked} 'Read more' buttons.", "progress": 50}
            await asyncio.sleep(1)
        
        MAX_PASSES = 3 if is_rescan else 2
        pass_num = 1
        saved_count = 0
        captured_files = []
        valid_elements = {}
        
        for idx, el in enumerate(comment_elements):
            valid_elements[idx] = el

        while pass_num <= MAX_PASSES:
            valid_comments_data = [] 
            
            for idx, el in valid_elements.items():
                if idx in exclude_ids:
                    continue
                text_el = el.locator("#content-text").first
                try:
                    if await text_el.is_visible():
                        comment_text = await text_el.inner_text()
                        comment_text = " ".join(comment_text.strip().split())
                        
                        if len(comment_text) >= MIN_COMMENT_LENGTH:
                            valid_comments_data.append({"id": idx, "text": comment_text})
                except:
                    pass

            if not valid_comments_data:
                if pass_num == 1:
                    yield {"type": "complete", "message": "더 이상 분석할 댓글이 없습니다.", "progress": 100, "images": []}
                    await browser.close()
                    return
                else:
                    break

            yield {"type": "log", "message": f"[Pass {pass_num}/{MAX_PASSES}] Extracted {len(valid_comments_data)} comments to analyze. AI filtering...", "progress": 55}

            ai_results = []
            if is_rescan or pass_num > 1:
                prompt_template = """
                당신은 유튜브 크리에이터의 '고객 후기 판독기'입니다.
                현재 제시된 댓글들은 이전 검토에서 누락되었을 수 있는 댓글들입니다. 
                기준을 조금 더 너그럽게 적용하여, 작성자가 영상이나 콘텐츠에 대해 긍정적인 뉘앙스를 보이거나 작게라도 도움을 받은 흔적이 있다면 **누락 없이 최대한 모두** 골라내세요.
                단순한 'ㅋㅋ', '잘봤습니다'라도 긍정적인 맥락이 길게 이어진다면 포함시킵니다.
                
                선별된 댓글에 대해 핵심 내용을 **10자 이내의 단어형태(명사형)**로 요약해주세요.
                출력은 반드시 순수 JSON 데이터 형식만 출력하세요. (백틱 ```json 제외)
                형식: [{"id": 0, "summary": "10자이내요약"}]
                (해당하는 댓글이 전혀 없다면 빈 배열 [] 을 반환하세요.)

                [대상 댓글 목록]
                """
                target_temperature = 0.7
            else:
                prompt_template = """
                당신은 유튜브 크리에이터의 '고객 후기 판독기'입니다.
                아래 나열된 댓글 목록을 읽고, 작성자가 '크리에이터의 영상, 서비스, 콘텐츠 등을 통해 실질적인 도움을 받았거나, 구체적인 긍정적 후기(예: 감탄, 지식 습득, 명쾌한 설명 등)'를 남긴 댓글만 골라내세요.
                단순한 'ㅋㅋ', '잘봤습니다' 등의 내용보다 구체적이고 긍정적인 감상평을 선호합니다.
                조건에 맞는 모든 댓글을 누락 없이 전부 찾아내세요.

                선별된 댓글에 대해 핵심 내용을 **10자 이내의 단어형태(명사형)**로 요약해주세요.
                출력은 반드시 순수 JSON 데이터 형식만 출력하세요. (백틱 ```json 제외)
                형식: [{"id": 0, "summary": "10자이내요약"}]
                (해당하는 댓글이 전혀 없다면 빈 배열 [] 을 반환하세요.)

                [대상 댓글 목록]
                """
                target_temperature = 0.2
            
            for i in range(0, len(valid_comments_data), BATCH_SIZE):
                batch = valid_comments_data[i:i+BATCH_SIZE]
                prompt = prompt_template + json.dumps(batch, ensure_ascii=False)
                
                yield {"type": "log", "message": f"[Pass {pass_num}/{MAX_PASSES}] AI Analyzing batch {i//BATCH_SIZE + 1} ({i+1} ~ {min(i+BATCH_SIZE, len(valid_comments_data))})...", "progress": 55 + min((i/len(valid_comments_data))*20, 20)}
                try:
                    response = client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            temperature=target_temperature,
                        ),
                    )
                    batch_results = json.loads(response.text)
                    ai_results.extend(batch_results)
                except Exception as e:
                    yield {"type": "log", "message": f"AI API Error: {e}"}

            if not ai_results:
                yield {"type": "log", "message": f"[Pass {pass_num}/{MAX_PASSES}] No additional positive comments found.", "progress": 80}
                break

            yield {"type": "log", "message": f"[Pass {pass_num}/{MAX_PASSES}] AI identified {len(ai_results)} positive comments. Starting screenshot capture...", "progress": 80}

            total_ai_results = len(ai_results)
            pass_saved = 0
            
            for idx, result in enumerate(ai_results):
                comment_id = result.get("id")
                summary = result.get("summary")
                
                if comment_id in valid_elements and comment_id not in exclude_ids:
                    el = valid_elements[comment_id]
                    
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.5) 
                    
                    safe_summary = re.sub(r'[\\/*?:"<>|]', "", str(summary)).strip()
                    safe_summary = safe_summary.replace(" ", "_")
                    filename = f"{safe_title}_{safe_summary}_{comment_id}.png"
                    filepath = os.path.join(output_dir, filename)
                    
                    target_comment_el = el.locator("ytd-comment-view-model").first
                    if await target_comment_el.count() == 0:
                        target_comment_el = el
                    
                    await target_comment_el.evaluate("node => { node.style.display = 'inline-block'; node.style.maxWidth = '100%'; node.style.backgroundColor = 'transparent'; }")
                    await target_comment_el.screenshot(path=filepath)
                    
                    padded, w, h, nw, nh = pad_image_to_aspect_ratio(filepath)
                    
                    exclude_ids.append(comment_id)
                    pass_saved += 1
                    saved_count += 1
                    
                    captured_files.append({
                        "filename": filename,
                        "url": f"/api/images/{task_id}/{filename}",
                        "summary": summary,
                        "padded": padded
                    })
                    
                    yield {"type": "log", "message": f"Captured screenshot: {filename}", "progress": 80 + min((idx/max(total_ai_results,1))*15, 15)}
                    
            if pass_saved == 0:
                break
                
            pass_num += 1
                
        yield {
            "type": "complete", 
            "message": f"Finished! {saved_count} screenshots saved perfectly padded.", 
            "progress": 100,
            "images": captured_files
        }
        await browser.close()
