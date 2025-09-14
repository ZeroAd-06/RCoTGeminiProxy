import os
import json
import copy
import httpx
import re
import time
from flask import Flask, request, Response, stream_with_context, jsonify


# --- 配置加载 (添加了 history_rewriting 的默认值) ---
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
    cfg = {
        "api_key": config.get("api_key", os.getenv("GEMINI_API_KEY")) or "",
        "api_base_url": config.get("api_base_url", "https://generativelanguage.googleapis.com"),
        "prompt_injection": config.get("prompt_injection", {}),
        "generation_prefix": config.get("generation_prefix", {}),
        "markers": config.get("markers", {}),
        "retry_mechanism": config.get("retry_mechanism", {"enabled": False}),
        "history_rewriting": config.get("history_rewriting", {"enabled": False})  # 默认禁用
    }
    cfg["markers"]["thought"] = cfg["markers"].get("thought", "_thought")
    cfg["markers"]["answer"] = cfg["markers"].get("answer", "_answer")
    return cfg


CONFIG = load_config()

# --- Flask 应用 ---
app = Flask(__name__)


# --- _split_and_yield 辅助函数 (无变化) ---
def _split_and_yield(text_to_process, is_thinking_ref, json_template):
    # ... (代码与上一版完全相同)
    thought_marker = CONFIG["markers"]["thought"]
    answer_marker = CONFIG["markers"]["answer"]
    end_marker = CONFIG.get("retry_mechanism", {}).get("end_marker")
    if end_marker and end_marker in text_to_process:
        text_to_process = text_to_process.replace(end_marker, "")
    thought_pattern = f"(?<!`){re.escape(thought_marker)}(?!`)"
    answer_pattern = f"(?<!`){re.escape(answer_marker)}(?!`)"
    while text_to_process:
        thought_match = re.search(thought_pattern, text_to_process)
        answer_match = re.search(answer_pattern, text_to_process)
        pos_thought = thought_match.start() if thought_match else -1
        pos_answer = answer_match.start() if answer_match else -1
        if pos_thought != -1 and (pos_thought < pos_answer or pos_answer == -1):
            pos_next_tag = pos_thought;
            next_tag = thought_marker
        elif pos_answer != -1:
            pos_next_tag = pos_answer;
            next_tag = answer_marker
        else:
            pos_next_tag = -1;
            next_tag = None
        segment = text_to_process if pos_next_tag == -1 else text_to_process[:pos_next_tag]
        if segment:
            new_json = copy.deepcopy(json_template)
            new_part = new_json['candidates'][0]['content']['parts'][0]
            new_part['text'] = segment
            if is_thinking_ref[0]:
                new_part['thought'] = True
            elif 'thought' in new_part:
                del new_part['thought']
            if not (pos_next_tag == -1) and 'finishReason' in new_json['candidates'][0]:
                del new_json['candidates'][0]['finishReason']
            yield f"data: {json.dumps(new_json)}\n\n"
        if next_tag:
            is_thinking_ref[0] = (next_tag == thought_marker)
            text_to_process = text_to_process[pos_next_tag + len(next_tag):]
        else:
            text_to_process = ""


# --- 主代理函数 (核心修改在此处) ---
@app.route('/v1beta/models/<path:model_name>:streamGenerateContent', methods=['POST'])
def proxy_stream_generate_content(model_name):
    request_data = request.get_json(silent=True)
    if not request_data:
        return jsonify({"error": "Request body must be valid JSON."}), 400

    initial_request = copy.deepcopy(request_data)
    contents = initial_request.get("contents", [])

    # --- 1. 历史重写逻辑 (无变化) ---
    history_config = CONFIG.get("history_rewriting", {})
    if history_config.get("enabled"):
        model_indices = [i for i, msg in enumerate(contents) if msg.get("role") == "model"]
        last_model_idx = model_indices[-1] if model_indices else -1
        for i in model_indices:
            if i == last_model_idx: continue
            msg = contents[i]
            if msg.get("parts") and msg["parts"][0].get("text"):
                original_text = msg["parts"][0]["text"]
                thought_marker = CONFIG["markers"]["thought"];
                answer_marker = CONFIG["markers"]["answer"]
                end_marker = CONFIG.get("retry_mechanism", {}).get("end_marker", "")
                placeholder = history_config.get("placeholder_text", "Thinking complete. Answer follows.")
                rewritten_text = (
                    f"{thought_marker}\n\n{placeholder}\n\n{answer_marker}\n\n{original_text}\n\n{end_marker}")
                contents[i]["parts"][0]["text"] = rewritten_text

    # --- 2. 注入逻辑 (已修改) ---
    prefix_to_prepend = ""
    # 首先找到最后一个 user 消息块作为操作的锚点
    last_user_idx = -1
    for i, msg in reversed(list(enumerate(contents))):
        if msg.get("role") == "user":
            last_user_idx = i
            break

    # 仅当上下文中存在 user 消息时才执行注入
    if last_user_idx != -1:
        # a) 将 prompt 注入为一个全新的 user 块
        if CONFIG["prompt_injection"].get("enabled"):
            injection_text = CONFIG["prompt_injection"].get("user_suffix", "")
            if injection_text:
                new_user_block = {"role": "user", "parts": [{"text": injection_text}]}
                # 插入到最后一个 user 块的后面
                contents.insert(last_user_idx + 1, new_user_block)
                print(f"INFO: Injected new user block at index {last_user_idx + 1}.")
                # 更新锚点，以便 model 块能正确插入到新 user 块的后面
                last_user_idx += 1

        # b) 将模型生成前缀注入为 model 块
        if CONFIG["generation_prefix"].get("enabled"):
            model_prefix = CONFIG["generation_prefix"].get("model_prefix", "")
            if model_prefix:
                prefix_message = {"role": "model", "parts": [{"text": model_prefix}]}
                # 插入到更新后的最后一个 user 块的后面
                contents.insert(last_user_idx + 1, prefix_message)
                prefix_to_prepend = model_prefix
                print(f"INFO: Injected model prefix at index {last_user_idx + 1}.")

    # --- 3. 调用 generate 函数 (与之前相同) ---
    def generate(initial_req, prefix):
        # ... (generate 函数的全部内容与上一版完全相同) ...
        retry_config = CONFIG.get("retry_mechanism", {})
        use_retry = retry_config.get("enabled", False)
        retries_left = retry_config.get("max_retries", 3) if use_retry else 0
        end_marker = retry_config.get("end_marker") if use_retry else None
        full_generated_text = ""
        is_thinking_ref = [False]
        prefix_sent = False
        last_disconnect_time = 0
        backoff_delay = retry_config.get("backoff_initial_seconds", 1)

        while True:
            current_request = copy.deepcopy(initial_req)
            if full_generated_text:
                print(f"INFO: Retrying... {retries_left} attempts left.")
                if time.time() - last_disconnect_time < retry_config.get("rapid_disconnect_threshold_seconds", 5):
                    print(f"INFO: Rapid disconnect detected. Backing off for {backoff_delay} seconds.")
                    time.sleep(backoff_delay)
                    backoff_delay *= retry_config.get("backoff_factor", 2)
                else:
                    backoff_delay = retry_config.get("backoff_initial_seconds", 1)
                model_indices = [i for i, msg in enumerate(current_request["contents"]) if msg.get("role") == "model"]
                if model_indices:
                    last_model_idx = model_indices[-1]
                    current_request["contents"][last_model_idx]["parts"][-1]["text"] = full_generated_text
            stream_finished_cleanly = False
            try:
                base_url = CONFIG["api_base_url"].rstrip('/')
                target_url = f"{base_url}/v1beta/models/{model_name}:streamGenerateContent?alt=sse"
                if CONFIG["api_key"]: target_url += f"&key={CONFIG['api_key']}"
                with httpx.stream("POST", target_url, json=current_request, timeout=180) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith('data: '):
                            json_str = line[6:]
                            try:
                                original_json = json.loads(json_str)
                                if not prefix_sent and prefix:
                                    prefix_template = {
                                        "candidates": [{"content": {"parts": [{"text": ""}], "role": "model"}}]}
                                    yield from _split_and_yield(prefix, is_thinking_ref, prefix_template)
                                    prefix_sent = True
                                text = original_json['candidates'][0]['content']['parts'][0].get('text', '')
                                full_generated_text += text
                                yield from _split_and_yield(text, is_thinking_ref, original_json)
                            except (json.JSONDecodeError, IndexError, KeyError):
                                yield f"data: {json_str}\n\n"
                        elif line:
                            yield f"{line}\n"
                stream_finished_cleanly = True
            except httpx.HTTPStatusError as e:
                print(f"ERROR: Upstream API returned status {e.response.status_code}. Content: {e.response.text}")
                yield f'data: {{"error": "Upstream API error", "status_code": {e.response.status_code}, "details": "{e.response.text}"}}\n\n'
                stream_finished_cleanly = False
            except httpx.RequestError as e:
                print(f"ERROR: Upstream request failed: {e}")
                stream_finished_cleanly = False
            last_disconnect_time = time.time()
            if use_retry:
                if stream_finished_cleanly and end_marker and full_generated_text.endswith(end_marker):
                    print("INFO: Clean exit marker found. Terminating.")
                    return
                if retries_left > 0:
                    retries_left -= 1
                    continue
                else:
                    print("ERROR: Max retries exceeded. Terminating.")
                    yield f'data: {{"error": "Max retries exceeded after stream interruption."}}\n\n'
                    return
            else:
                return

    return Response(stream_with_context(generate(initial_request, prefix_to_prepend)), content_type='text/event-stream')


if __name__ == '__main__':
    print("Configuration loaded:")
    print(json.dumps(CONFIG, indent=2))
    app.run(host='0.0.0.0', port=5000, debug=True)