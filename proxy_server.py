import os
import json
import copy
import httpx
import re
from flask import Flask, request, Response, stream_with_context, jsonify


# --- 配置加载 ---
def load_config():
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
    cfg = {
        "api_key": config.get("api_key", os.getenv("GEMINI_API_KEY")) or "",
        "api_base_url": config.get("api_base_url", "https://generativelanguage.googleapis.com"),
        "prompt_injection": config.get("prompt_injection", {}),
        "generation_prefix": config.get("generation_prefix", {}),
        "markers": config.get("markers", {})
    }
    cfg["markers"]["thought"] = cfg["markers"].get("thought", "_thought")
    cfg["markers"]["answer"] = cfg["markers"].get("answer", "_answer")
    return cfg


CONFIG = load_config()

# --- Flask 应用 ---
app = Flask(__name__)


def _split_and_yield(text_to_process, is_thinking_ref, json_template):
    thought_marker = CONFIG["markers"]["thought"]
    answer_marker = CONFIG["markers"]["answer"]

    thought_pattern = f"(?<!`){re.escape(thought_marker)}(?!`)"
    answer_pattern = f"(?<!`){re.escape(answer_marker)}(?!`)"

    while text_to_process:
        thought_match = re.search(thought_pattern, text_to_process)
        answer_match = re.search(answer_pattern, text_to_process)

        pos_thought = thought_match.start() if thought_match else -1
        pos_answer = answer_match.start() if answer_match else -1

        if pos_thought != -1 and (pos_thought < pos_answer or pos_answer == -1):
            pos_next_tag = pos_thought
            next_tag = thought_marker
        elif pos_answer != -1:
            pos_next_tag = pos_answer
            next_tag = answer_marker
        else:
            pos_next_tag = -1
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
            is_last_segment = (pos_next_tag == -1)
            if not is_last_segment and 'finishReason' in new_json['candidates'][0]:
                del new_json['candidates'][0]['finishReason']
            yield f"data: {json.dumps(new_json)}\n\n"

        if next_tag:
            is_thinking_ref[0] = (next_tag == thought_marker)
            text_to_process = text_to_process[pos_next_tag + len(next_tag):]
        else:
            text_to_process = ""


# --- 主代理函数 ---
@app.route('/v1beta/models/<path:model_name>:streamGenerateContent', methods=['POST'])
def proxy_stream_generate_content(model_name):
    request_data = request.get_json(silent=True)
    if not request_data:
        return jsonify({"error": "Request body must be valid JSON."}), 400

    modified_request = copy.deepcopy(request_data)
    contents = modified_request.get("contents", [])
    prefix_to_prepend = ""
    last_user_msg_idx = -1
    for i, msg in reversed(list(enumerate(contents))):
        if msg.get("role") == "user":
            last_user_msg_idx = i
            break
    if last_user_msg_idx != -1 and CONFIG["prompt_injection"].get("enabled"):
        user_suffix = CONFIG["prompt_injection"].get("user_suffix", "")
        if user_suffix:
            if contents[last_user_msg_idx].get("parts"):
                contents[last_user_msg_idx]["parts"][-1]["text"] += user_suffix
            print(f"INFO: Injected user suffix.")
    if last_user_msg_idx != -1 and CONFIG["generation_prefix"].get("enabled"):
        model_prefix = CONFIG["generation_prefix"].get("model_prefix", "")
        if model_prefix:
            prefix_message = {"role": "model", "parts": [{"text": model_prefix}]}
            contents.insert(last_user_msg_idx + 1, prefix_message)
            prefix_to_prepend = model_prefix
            print(f"INFO: Injected model prefix.")

    base_url = CONFIG["api_base_url"].rstrip('/')
    target_url = f"{base_url}/v1beta/models/{model_name}:streamGenerateContent?alt=sse"
    if CONFIG["api_key"]:
        target_url += f"&key={CONFIG['api_key']}"

    def generate(upstream_data, prefix):
        is_thinking_ref = [False]
        if prefix:
            print("INFO: Prepending generation prefix to response stream.")
            prefix_template = {"candidates": [{"content": {"parts": [{"text": ""}], "role": "model"}}]}
            yield from _split_and_yield(prefix, is_thinking_ref, prefix_template)

        print(f"INFO: Connecting to upstream: {target_url}")
        try:
            with httpx.stream(
                    "POST", target_url, json=upstream_data,
                    headers={'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
                    timeout=180
            ) as response:
                print(f"INFO: Upstream responded with status code: {response.status_code}")
                if response.status_code != 200:
                    error_body = response.read()
                    yield f'data: {{"error": "Upstream API error", "status_code": {response.status_code}, "details": {json.dumps(error_body.decode("utf-8"))}}}\n\n'
                    return
                for line in response.iter_lines():
                    if line.startswith('data: '):
                        json_str = line[6:]
                        try:
                            original_json = json.loads(json_str)
                            text = original_json['candidates'][0]['content']['parts'][0].get('text', '')
                            yield from _split_and_yield(text, is_thinking_ref, original_json)
                        except (json.JSONDecodeError, IndexError, KeyError):
                            yield f"data: {json_str}\n\n"
                    elif line:
                        yield f"{line}\n"
        except httpx.RequestError as e:
            print(f"ERROR: Upstream request failed: {e}")
            yield f'data: {{"error": "Upstream request failed", "details": "{str(e)}"}}\n\n'
        print("INFO: Stream finished.")

    return Response(stream_with_context(generate(modified_request, prefix_to_prepend)),
                    content_type='text/event-stream')


if __name__ == '__main__':
    print("Configuration loaded:")
    print(json.dumps(CONFIG, indent=2))
    app.run(host='0.0.0.0', port=5000, debug=True)