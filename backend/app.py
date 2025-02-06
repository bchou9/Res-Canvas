import json
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import redis
from config import RESDB_API_COMMIT, RESDB_API_QUERY, HEADERS

lock = threading.Lock()

app = Flask(__name__)
CORS(app)  # Enable global CORS

# Initialize Redis client
redis_client = redis.Redis(host='localhost', port=6379, db=0)

# Function to get the canvas drawing count
def get_canvas_draw_count():
    count = redis_client.get('res-canvas-draw-count')

    if count is None:
        # If not in Redis, get from external API
        response = requests.get(
            RESDB_API_QUERY + "res-canvas-draw-count", headers=HEADERS)
        if response.status_code // 100 == 2:
            count = int(response.json()['value'])
            redis_client.set('res-canvas-draw-count', count)
        else:
            raise KeyError("Failed to get canvas draw count.")
    else:
        count = int(count)
    return count

# Function to increment the canvas drawing count
def increment_canvas_draw_count():
    with lock:
        count = get_canvas_draw_count() + 1
        # Update in Redis
        redis_client.set('res-canvas-draw-count', count)
        # Update in external API
        increment_count = {"id": "res-canvas-draw-count", "value": count}
        response = requests.post(
            RESDB_API_COMMIT, json=increment_count, headers=HEADERS)
        if response.status_code // 100 != 2:
            raise KeyError("Failed to increment canvas draw count.")
    return count

# POST endpoint: AddClearTimestamp
@app.route('/submitClearCanvasTimestamp', methods=['POST'])
def submit_clear_timestamp():
    try:
        # Ensure the request has JSON data
        if not request.is_json:
            return jsonify({
                "status": "error",
                "message": "Request Content-Type must be 'application/json'."
            }), 400

        request_data = request.json
        if not request_data:
            return jsonify({"status": "error", "message": "Invalid input"}), 400

        # Validate required fields
        if 'ts' not in request_data:
            return jsonify({"status": "error", "message": "Missing required fields: ts"}), 400

        request_data['id'] = 'clear-canvas-timestamp'
        # print("request_data:")
        print(request_data)
        response = requests.post(
            RESDB_API_COMMIT, json=request_data, headers=HEADERS)

        if response.status_code // 100 == 2:
            # Cache the new timestamp in Redis
            redis_client.set(request_data['id'], request_data["ts"])

            # Clear all undo/redo stacks in Redis
            for key in redis_client.scan_iter("*:undo"):
                redis_client.delete(key)
            for key in redis_client.scan_iter("*:redo"):
                redis_client.delete(key)

            return jsonify({"status": "success", "message": "timestamp submitted successfully"}), 201
        else:
            raise KeyError("Failed to submit data to external API.")
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# POST endpoint: submitNewLine
@app.route('/submitNewLine', methods=['POST'])
def submit_new_line():
    try:
        # Ensure the request has JSON data
        if not request.is_json:
            return jsonify({
                "status": "error",
                "message": "Request Content-Type must be 'application/json'."
            }), 400

        request_data = request.json
        user_id = request_data.get("user")
        if not request_data:
            return jsonify({"status": "error", "message": "Invalid input"}), 400

        # Validate required fields
        if 'ts' not in request_data or 'value' not in request_data or 'user' not in request_data:
            return jsonify({"status": "error", "message": "Missing required fields: ts, value or user"}), 400

        # Get the canvas drawing count and increment it
        res_canvas_draw_count = get_canvas_draw_count()

        request_data['id'] = "res-canvas-draw-" + \
            str(res_canvas_draw_count)  # Adjust index
        # Ensure new strokes are marked as not undone
        request_data['undone'] = False
        print("submit_new_lineZZrequest_data:")
        print(request_data)

        # Forward the data to the external API
        response = requests.post(
            RESDB_API_COMMIT, json=request_data, headers=HEADERS)

        # Check response status
        if response.status_code // 100 == 2:
            # Cache the new drawing in Redis
            increment_canvas_draw_count()
            redis_client.set(request_data['id'], json.dumps(request_data))
            #redis_client.rpush("global:drawings", json.dumps(request_data))

            # Update user's undo/redo stacks
            redis_client.lpush(f"{user_id}:undo", json.dumps(request_data))
            redis_client.delete(f"{user_id}:redo")  # Clear redo stack

            return jsonify({"status": "success", "message": "Line submitted successfully"}), 201
        else:
            raise KeyError("Failed to submit data to external API.")
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# GET endpoint: getCanvasData
@app.route('/getCanvasData', methods=['GET'])
def get_canvas_data():
    try:
        from_ = request.args.get('from')
        # print("from_:", from_)
        if from_ is None:
            return jsonify({"status": "error", "message": "Missing required fields: from"}), 400
        from_ = int(from_)

        res_canvas_draw_count = get_canvas_draw_count()
        # print("res_canvas_draw_count:", res_canvas_draw_count)
        # Ensure clear_timestamp exists, defaulting to 0 if not found
        # TODO: move to get_clear_timestamp()
        clear_timestamp = redis_client.get('clear-canvas-timestamp')
        if clear_timestamp is None:
            response = requests.get(RESDB_API_QUERY + "clear-canvas-timestamp")
            if response.status_code == 200 and response.text:
                clear_timestamp = int(response.json().get("ts", 0))
                redis_client.set("clear-canvas-timestamp", clear_timestamp)
                print("clear_timestamp_RESDB:", clear_timestamp)
            else:
                clear_timestamp = 0
                print("clear_timestamp_RESDB_ERROR:", clear_timestamp)
        else:
            clear_timestamp = int(clear_timestamp.decode())
            print("clear_timestamp_REDIS:", clear_timestamp)

        all_missing_data = []
        missing_keys = []
        redone_strokes = set()
        undone_strokes = set()
        
        # Fetch redone strokes from Redis
        for key in redis_client.keys("redo-*"):
            redone_data = redis_client.get(key)
            
            if redone_data:
                redone_strokes.add(json.loads(redone_data)[
                                   "id"].replace("redo-", ""))
        print("redone_strokes", redone_strokes)

        # Fetch undone strokes from Redis
        for key in redis_client.keys("undo-*"):
            undone_data = redis_client.get(key)
            unloaded_data_from_json = json.loads(undone_data)["id"].replace("undo-", "")
            print("unloaded_data_from_json: ", unloaded_data_from_json)
            if undone_data and unloaded_data_from_json not in redone_strokes:
                undone_strokes.add(unloaded_data_from_json)
        print("undone_strokes", undone_strokes)

        # Check Redis for existing data
        for i in range(from_, res_canvas_draw_count):
            key_id = "res-canvas-draw-" + str(i)
            data = redis_client.get(key_id)
            if data:
                drawing = json.loads(data)
                # print("IN: ", key_id)
                # Exclude undone strokes
                if drawing["id"] not in undone_strokes and "ts" in drawing and isinstance(drawing["ts"], int) and drawing["ts"] > clear_timestamp:
                    all_missing_data.append(drawing)
            else:
                # print("OUT: ", key_id)
                missing_keys.append((key_id, i))

        # Fetch missing data from ResDB
        for key_id, index in missing_keys:
            response = requests.get(RESDB_API_QUERY + key_id)
            if response.status_code == 200 and response.text:
                if response.headers.get("Content-Type") == "application/json":
                    data = response.json()
                    redis_client.set(key_id, json.dumps(data))

                    # Exclude undone strokes
                    if data["id"] not in undone_strokes and "ts" in data and isinstance(data["ts"], int) and data["ts"] > clear_timestamp:
                        all_missing_data.append(data)

                    print("key_id", key_id)
                    print("data", data)

        # Now check for undone strokes stored in resdb but not in redis to prevent them from loading back
        stroke_entries = {}
        for entry in all_missing_data:
            stroke_id = entry.get('id')
            time_stamp = entry.get('ts')
            print("stroke_id: ", stroke_id)
            print("time_stamp: ", time_stamp)
            if stroke_id and time_stamp:
                existing_entry = stroke_entries.get(stroke_id)
                if not existing_entry or time_stamp > existing_entry['ts']:
                    stroke_entries[stroke_id] = entry
        
        # Filter out entries where 'undone' is True for the latest entry
        active_strokes = [entry for entry in stroke_entries.values() if not entry.get('undone', False)]
        all_missing_data = active_strokes
        
        all_missing_data.sort(key=lambda x: int(x["id"].split("-")[-1]))
        return jsonify({"status": "success", "data": all_missing_data}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# TODO: check if working later
@app.route('/checkUndoRedo', methods=['GET'])
def check_undo_redo():
    user_id = request.args.get("userId")
    if not user_id:
        return jsonify({"status": "error", "message": "User ID required"}), 400

    # Fetch undo/redo stacks from Redis
    undo_available = redis_client.llen(f"{user_id}:undo") > 0
    redo_available = redis_client.llen(f"{user_id}:redo") > 0

    # if not undo_available:
    #     response = requests.get(RESDB_API_QUERY + f"undo-{user_id}")
    #     if response.status_code == 200 and response.text:
    #         undo_available = True  # Found an undo record in ResDB

    # if not redo_available:
    #     response = requests.get(RESDB_API_QUERY + f"redo-{user_id}")
    #     if response.status_code == 200 and response.text:
    #         redo_available = True  # Found a redo record in ResDB

    return jsonify({"undoAvailable": undo_available, "redoAvailable": redo_available}), 200

# POST endpoint: Undo operation
@app.route('/undo', methods=['POST'])
def undo_action():
    try:
        data = request.json
        user_id = data.get("userId")
        if not user_id:
            return jsonify({"status": "error", "message": "User ID required"}), 400

        undo_stack = redis_client.lrange(f"{user_id}:undo", 0, -1)
        if not undo_stack:
            return jsonify({"status": "error", "message": "Nothing to undo"}), 400

        last_action = redis_client.lpop(f"{user_id}:undo")
        redis_client.lpush(f"{user_id}:redo", last_action)

        last_action_data = json.loads(last_action)
        undo_record = {
            "id": f"undo-{last_action_data['id']}",
            "ts": int(time.time() * 1000),
            "user": user_id,
            "undone": True,
            "value": json.dumps(last_action_data)
        }
        redis_client.set(undo_record["id"], json.dumps(undo_record))
        #redis_client.lrem("global:drawings", 1, last_action)
        last_action_data['undone'] = True
        last_action_data['ts'] = int(time.time() * 1000)
        print("last_action_data_UNDO:", last_action_data)
        response = requests.post(
            RESDB_API_COMMIT, json=last_action_data, headers=HEADERS)
        if response.status_code // 100 != 2:
            raise KeyError("Failed to append undo action in ResDB.")

        return jsonify({"status": "success", "message": "Undo successful"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# POST endpoint: Redo operation
@app.route('/redo', methods=['POST'])
def redo_action():
    try:
        data = request.json
        user_id = data.get("userId")
        if not user_id:
            return jsonify({"status": "error", "message": "User ID required"}), 400

        redo_stack = redis_client.lrange(f"{user_id}:redo", 0, -1)
        if not redo_stack:
            return jsonify({"status": "error", "message": "Nothing to redo"}), 400

        last_action = redis_client.lpop(f"{user_id}:redo")
        redis_client.lpush(f"{user_id}:undo", last_action)

        last_action_data = json.loads(last_action)
        redo_record = {
            "id": f"redo-{last_action_data['id']}",
            "ts": int(time.time() * 1000),
            "user": user_id,
            "undone": False,
            "value": json.dumps(last_action_data)
        }

        redis_client.set(redo_record["id"], json.dumps(redo_record))
        #redis_client.rpush("global:drawings", last_action)
        last_action_data['undone'] = False
        last_action_data['ts'] = int(time.time() * 1000)
        print("last_action_data_REDO", last_action_data)
        response = requests.post(
            RESDB_API_COMMIT, json=last_action_data, headers=HEADERS)
        if response.status_code // 100 != 2:
            raise KeyError("Failed to append redo action in ResDB.")

        return jsonify({"status": "success", "message": "Redo successful"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    # TODO: merge to get_canvas_draw_count()
    # since need to fetch from ResDB instead

    # Initialize res-canvas-draw-count if not present in Redis
    if not redis_client.exists('res-canvas-draw-count'):
        init_count = {"id": "res-canvas-draw-count", "value": 0}
        print("Initialize res-canvas-draw-count if not present in Redis: ", init_count)
        response = requests.post(
            RESDB_API_COMMIT, json=init_count, headers=HEADERS)
        if response.status_code // 100 == 2:
            redis_client.set('res-canvas-draw-count', 0)
            print('Set res-canvas-draw-count response:', response)
            app.run(debug=True, host="0.0.0.0", port=10010)
        else:
            print('Set res-canvas-draw-count response:', response)
    else:
        app.run(debug=True, host="0.0.0.0", port=10010)
