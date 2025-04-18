from flask import Flask, request, jsonify, Response
import configparser  # Add this with your other imports
from flask_sse import sse
import redis

from flask_cors import CORS
import os
import sqlite3
from datetime import datetime
import json
from pathlib import Path

import yaml

from PIL import Image
from PIL import ImageFile

from npcsh.command_history import (
    CommandHistory,
    save_conversation_message,
)
from npcsh.npc_compiler import NPCCompiler, Tool, NPC
from npcsh.npc_sysenv import (
    get_model_and_provider,
    get_available_models,
    get_system_message,
    NPCSH_STREAM_OUTPUT,
)


from npcsh.llm_funcs import (
    check_llm_command,
    get_llm_response,
    get_stream,
    get_conversation,
)
from npcsh.helpers import get_directory_npcs, get_db_npcs, get_npc_path
from npcsh.npc_compiler import load_npc_from_file
from npcsh.shell_helpers import execute_command, execute_command_stream
import base64

import json
import os
from pathlib import Path

# Path for storing settings
SETTINGS_FILE = Path(os.path.expanduser("~/.npcshrc"))

# Configuration
db_path = os.path.expanduser("~/npcsh_history.db")
user_npc_directory = os.path.expanduser("~/.npcsh/npc_team")
project_npc_directory = os.path.abspath("./npc_team")

# Initialize components
npc_compiler = NPCCompiler(user_npc_directory, db_path)

app = Flask(__name__)
app.config["REDIS_URL"] = "redis://localhost:6379"
app.register_blueprint(sse, url_prefix="/stream")

redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)

CORS(
    app,
    origins=["http://localhost:5173"],
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    supports_credentials=True,
)


def get_locally_available_models(project_directory):
    # check if anthropic, gemini, openai keys exist in project folder env
    # also try to get ollama
    available_models_providers = []
    # get the project env
    env_path = os.path.join(project_directory, ".env")
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if "=" in line:
                        key, value = line.split("=", 1)
                        env_vars[key.strip()] = value.strip().strip("\"'")
    # check if the keys exist in the env
    if "ANTHROPIC_API_KEY" in env_vars:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        models = client.models.list()
        for model in models.data:

            available_models_providers.append(
                {
                    "model": model.id,
                    "provider": "anthropic",
                }
            )

    if "OPENAI_API_KEY" in env_vars:
        import openai

        openai.api_key = env_vars["OPENAI_API_KEY"]
        models = openai.models.list()

        for model in models.data:
            if (
                (
                    "gpt" in model.id
                    or "o1" in model.id
                    or "o3" in model.id
                    or "chat" in model.id
                )
                and "audio" not in model.id
                and "realtime" not in model.id
            ):

                available_models_providers.append(
                    {
                        "model": model.id,
                        "provider": "openai",
                    }
                )
    if "GEMINI_API_KEY" in env_vars:
        import google.generativeai as gemini

        gemini.configure(api_key=env_vars["GEMINI_API_KEY"])
        models = gemini.list_models()
        # available_models_providers.append(
        #    {
        #        "model": "gemini-2.5-pro",
        #        "provider": "gemini",
        #    }
        # )
        available_models_providers.append(
            {
                "model": "gemini-2.0-flash-lite",
                "provider": "gemini",
            }
        )

    if "DEEPSEEK_API_KEY" in env_vars:
        available_models_providers.append(
            {"model": "deepseek-chat", "provider": "deepseek"}
        )
        available_models_providers.append(
            {"model": "deepseek-reasoner", "provider": "deepseek"}
        )

    try:
        import ollama

        models = ollama.list()
        for model in models:
            if "embed" not in model.model:
                mod = model.model
                available_models_providers.append(
                    {
                        "model": mod,
                        "provider": "ollama",
                    }
                )

    except Exception as e:
        print(f"Error loading ollama models: {e}")
    return available_models_providers


def get_db_connection():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


extension_map = {
    "PNG": "images",
    "JPG": "images",
    "JPEG": "images",
    "GIF": "images",
    "SVG": "images",
    "MP4": "videos",
    "AVI": "videos",
    "MOV": "videos",
    "WMV": "videos",
    "MPG": "videos",
    "MPEG": "videos",
    "DOC": "documents",
    "DOCX": "documents",
    "PDF": "documents",
    "PPT": "documents",
    "PPTX": "documents",
    "XLS": "documents",
    "XLSX": "documents",
    "TXT": "documents",
    "CSV": "documents",
    "ZIP": "archives",
    "RAR": "archives",
    "7Z": "archives",
    "TAR": "archives",
    "GZ": "archives",
    "BZ2": "archives",
    "ISO": "archives",
}


def fetch_messages_for_conversation(conversation_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
        SELECT role, content
        FROM conversation_history
        WHERE conversation_id = ?
        ORDER BY timestamp ASC
    """
    cursor.execute(query, (conversation_id,))
    messages = cursor.fetchall()
    conn.close()

    return [
        {
            "role": message["role"],
            "content": message["content"],
        }
        for message in messages
    ]


@app.route("/api/attachments/<message_id>", methods=["GET"])
def get_message_attachments(message_id):
    """Get all attachments for a message"""
    try:
        command_history = CommandHistory(db_path)
        attachments = command_history.get_message_attachments(message_id)
        return jsonify({"attachments": attachments, "error": None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/attachment/<attachment_id>", methods=["GET"])
def get_attachment(attachment_id):
    """Get specific attachment data"""
    try:
        command_history = CommandHistory(db_path)
        data, name, type = command_history.get_attachment_data(attachment_id)

        if data:
            # Convert binary data to base64 for sending
            base64_data = base64.b64encode(data).decode("utf-8")
            return jsonify(
                {"data": base64_data, "name": name, "type": type, "error": None}
            )
        return jsonify({"error": "Attachment not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/capture_screenshot", methods=["GET"])
def capture():
    # Capture screenshot using NPC-based method
    screenshot = capture_screenshot(None, full=True)

    # Ensure screenshot was captured successfully
    if not screenshot:
        print("Screenshot capture failed")
        return None

    return jsonify({"screenshot": screenshot})


@app.route("/api/settings/global", methods=["GET", "OPTIONS"])
def get_global_settings():
    if request.method == "OPTIONS":
        return "", 200

    try:
        npcshrc_path = os.path.expanduser("~/.npcshrc")

        # Default settings
        global_settings = {
            "model": "llama3.2",
            "provider": "ollama",
            "embedding_model": "nomic-embed-text",
            "embedding_provider": "ollama",
            "search_provider": "google",
            "NPCSH_LICENSE_KEY": "",
        }
        global_vars = {}

        if os.path.exists(npcshrc_path):
            with open(npcshrc_path, "r") as f:
                for line in f:
                    # Skip comments and empty lines
                    line = line.split("#")[0].strip()
                    if not line:
                        continue

                    if "=" not in line:
                        continue

                    # Split on first = only
                    key, value = line.split("=", 1)
                    key = key.strip()
                    if key.startswith("export "):
                        key = key[7:]

                    # Clean up the value - handle quoted strings properly
                    value = value.strip()
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]

                    # Map environment variables to settings
                    key_mapping = {
                        "NPCSH_MODEL": "model",
                        "NPCSH_PROVIDER": "provider",
                        "NPCSH_EMBEDDING_MODEL": "embedding_model",
                        "NPCSH_EMBEDDING_PROVIDER": "embedding_provider",
                        "NPCSH_SEARCH_PROVIDER": "search_provider",
                        "NPCSH_LICENSE_KEY": "NPCSH_LICENSE_KEY",
                        "NPCSH_STREAM_OUTPUT": "NPCSH_STREAM_OUTPUT",
                    }

                    if key in key_mapping:
                        global_settings[key_mapping[key]] = value
                    else:
                        global_vars[key] = value

        return jsonify(
            {
                "global_settings": global_settings,
                "global_vars": global_vars,
                "error": None,
            }
        )

    except Exception as e:
        print(f"Error in get_global_settings: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/global", methods=["POST", "OPTIONS"])
def save_global_settings():
    if request.method == "OPTIONS":
        return "", 200

    try:
        data = request.json
        npcshrc_path = os.path.expanduser("~/.npcshrc")

        key_mapping = {
            "model": "NPCSH_CHAT_MODEL",
            "provider": "NPCSH_CHAT_PROVIDER",
            "embedding_model": "NPCSH_EMBEDDING_MODEL",
            "embedding_provider": "NPCSH_EMBEDDING_PROVIDER",
            "search_provider": "NPCSH_SEARCH_PROVIDER",
            "NPCSH_LICENSE_KEY": "NPCSH_LICENSE_KEY",
            "NPCSH_STREAM_OUTPUT": "NPCSH_STREAM_OUTPUT",
        }

        os.makedirs(os.path.dirname(npcshrc_path), exist_ok=True)

        with open(npcshrc_path, "w") as f:
            # Write settings as environment variables
            for key, value in data.get("global_settings", {}).items():
                if key in key_mapping and value:
                    # Quote value if it contains spaces
                    if " " in str(value):
                        value = f'"{value}"'
                    f.write(f"export {key_mapping[key]}={value}\n")

            # Write custom variables
            for key, value in data.get("global_vars", {}).items():
                if key and value:
                    if " " in str(value):
                        value = f'"{value}"'
                    f.write(f"export {key}={value}\n")

        return jsonify({"message": "Global settings saved successfully", "error": None})

    except Exception as e:
        print(f"Error in save_global_settings: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/project", methods=["GET", "OPTIONS"])  # Add OPTIONS
def get_project_settings():
    if request.method == "OPTIONS":
        return "", 200

    try:
        current_dir = request.args.get("path")
        if not current_dir:
            return jsonify({"error": "No path provided"}), 400

        env_path = os.path.join(current_dir, ".env")
        env_vars = {}

        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if "=" in line:
                            key, value = line.split("=", 1)
                            env_vars[key.strip()] = value.strip().strip("\"'")

        return jsonify({"env_vars": env_vars, "error": None})

    except Exception as e:
        print(f"Error in get_project_settings: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/project", methods=["POST", "OPTIONS"])  # Add OPTIONS
def save_project_settings():
    if request.method == "OPTIONS":
        return "", 200

    try:
        current_dir = request.args.get("path")
        if not current_dir:
            return jsonify({"error": "No path provided"}), 400

        data = request.json
        env_path = os.path.join(current_dir, ".env")

        with open(env_path, "w") as f:
            for key, value in data.get("env_vars", {}).items():
                f.write(f"{key}={value}\n")

        return jsonify(
            {"message": "Project settings saved successfully", "error": None}
        )

    except Exception as e:
        print(f"Error in save_project_settings: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/models", methods=["GET"])
def get_models():
    """
    Endpoint to retrieve available models based on the current project path.
    Checks for local configurations (.env) and Ollama.
    """
    current_path = request.args.get("currentPath")
    if not current_path:
        # Fallback to a default path or user home if needed,
        # but ideally the frontend should always provide it.
        current_path = os.path.expanduser("~/.npcsh")  # Or handle error
        print("Warning: No currentPath provided for /api/models, using default.")
        # return jsonify({"error": "currentPath parameter is required"}), 400

    try:
        # Reuse the existing function to detect models
        available_models = get_locally_available_models(current_path)

        # Optionally, add more details or format the response if needed
        # Example: Add a display name
        formatted_models = []
        for m in available_models:
            # Basic formatting, customize as needed
            text_only = (
                "(text only)"
                if m["provider"] == "ollama"
                and m["model"] in ["llama3.2", "deepseek-v3", "phi4"]
                else ""
            )
            # Handle specific known model names for display
            display_model = m["model"]
            if "claude-3-5-haiku-latest" in m["model"]:
                display_model = "claude-3.5-haiku"
            elif "claude-3-5-sonnet-latest" in m["model"]:
                display_model = "claude-3.5-sonnet"
            elif "gemini-1.5-flash" in m["model"]:
                display_model = "gemini-1.5-flash"  # Handle multiple versions if needed
            elif "gemini-2.0-flash-lite-preview-02-05" in m["model"]:
                display_model = "gemini-2.0-flash-lite-preview"

            display_name = f"{display_model} | {m['provider']} {text_only}".strip()

            formatted_models.append(
                {
                    "value": m["model"],  # Use the actual model ID as the value
                    "provider": m["provider"],
                    "display_name": display_name,
                }
            )

        return jsonify({"models": formatted_models, "error": None})

    except Exception as e:
        print(f"Error getting available models: {str(e)}")
        import traceback

        traceback.print_exc()
        # Return an empty list or a specific error structure
        return jsonify({"models": [], "error": str(e)}), 500


@app.route("/api/stream", methods=["POST"])
def stream():
    """SSE stream that takes messages, models, providers, and attachments from frontend."""
    data = request.json
    commandstr = data.get("commandstr")
    conversation_id = data.get("conversationId")
    model = data.get("model", None)
    provider = data.get("provider", None)
    npc = data.get("npc", None)
    attachments = data.get("attachments", [])
    current_path = data.get("currentPath")

    command_history = CommandHistory(db_path)

    # Process attachments and save them properly
    images = []
    print(attachments)

    from io import BytesIO
    from PIL import Image

    attachments_loaded = []

    if attachments:
        for attachment in attachments:
            extension = attachment["name"].split(".")[-1]
            extension_mapped = extension_map.get(extension.upper(), "others")
            file_path = os.path.expanduser(
                "~/.npcsh/" + extension_mapped + "/" + attachment["name"]
            )

            if extension_mapped == "images":
                # Open the image file and save it to the file path
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                img = Image.open(attachment["path"])

                # Save the image to a BytesIO buffer (to extract binary data)
                img_byte_arr = BytesIO()
                img.save(img_byte_arr, format="PNG")  # or the appropriate format
                img_byte_arr.seek(0)  # Rewind the buffer to the beginning

                # Save the image to a file
                img.save(file_path, optimize=True, quality=50)

                # Add to images list for LLM processing
                images.append({"filename": attachment["name"], "file_path": file_path})

                # Add the image data (in binary form) to attachments_loaded
                attachments_loaded.append(
                    {
                        "name": attachment["name"],
                        "type": extension_mapped,
                        "data": img_byte_arr.read(),  # Read binary data from the buffer
                        "size": os.path.getsize(file_path),
                    }
                )

    messages = fetch_messages_for_conversation(conversation_id)
    messages.append({"role": "user", "content": commandstr})
    if not messages:
        return jsonify({"error": "No messages provided"}), 400

    # Save the user message with attachments in the database
    print("commandstr ", commandstr)
    message_id = command_history.generate_message_id()

    save_conversation_message(
        command_history,
        conversation_id,
        "user",
        commandstr,
        wd=current_path,
        model=model,
        provider=provider,
        npc=npc,
        attachments=attachments_loaded,
        message_id=message_id,
    )
    message_id = command_history.generate_message_id()

    stream_response = get_stream(
        messages,
        images=images,
        model=model,
        provider=provider,
        npc=npc if isinstance(npc, NPC) else None,
    )

    final_response = ""  # To accumulate the assistant's response

    complete_response = []  # List to store all chunks

    def event_stream():
        for response_chunk in stream_response:
            chunk_content = ""
            chunk_content = "".join(
                choice.delta.content
                for choice in response_chunk.choices
                if choice.delta.content is not None
            )
            if chunk_content:
                complete_response.append(chunk_content)
            chunk_data = {
                "id": response_chunk.id,
                "object": response_chunk.object,
                "created": response_chunk.created,
                "model": response_chunk.model,
                "choices": [
                    {
                        "index": choice.index,
                        "delta": {
                            "content": choice.delta.content,
                            "role": choice.delta.role,
                        },
                        "finish_reason": choice.finish_reason,
                    }
                    for choice in response_chunk.choices
                ],
            }
            yield f"data: {json.dumps(chunk_data)}\n\n"
            save_conversation_message(
                command_history,
                conversation_id,
                "assistant",
                chunk_content,
                wd=current_path,
                model=model,
                provider=provider,
                npc=npc,
                message_id=message_id,  # Save with the same message_id
            )

        # Send completion message
        yield f"data: {json.dumps({'type': 'message_stop'})}\n\n"
        full_content = command_history.get_full_message_content(message_id)
        command_history.update_message_content(message_id, full_content)

    response = Response(event_stream(), mimetype="text/event-stream")

    return response


@app.route("/api/npc_team_global")
def get_npc_team_global():
    try:
        db_conn = get_db_connection()
        global_npc_directory = os.path.expanduser("~/.npcsh/npc_team")

        npc_data = []

        # Use existing helper to get NPCs from the global directory
        for npc_file in os.listdir(global_npc_directory):
            if npc_file.endswith(".npc"):
                npc_path = os.path.join(global_npc_directory, npc_file)
                npc = load_npc_from_file(npc_path, db_conn)

                # Serialize the NPC data
                serialized_npc = {
                    "name": npc.name,
                    "primary_directive": npc.primary_directive,
                    "model": npc.model,
                    "provider": npc.provider,
                    "api_url": npc.api_url,
                    "use_global_tools": npc.use_global_tools,
                    "tools": [
                        {
                            "tool_name": tool.tool_name,
                            "inputs": tool.inputs,
                            "preprocess": tool.preprocess,
                            "prompt": tool.prompt,
                            "postprocess": tool.postprocess,
                        }
                        for tool in npc.tools
                    ],
                }
                npc_data.append(serialized_npc)

        return jsonify({"npcs": npc_data, "error": None})

    except Exception as e:
        print(f"Error loading global NPCs: {str(e)}")
        return jsonify({"npcs": [], "error": str(e)})


@app.route("/api/tools/global", methods=["GET"])
def get_global_tools():
    # try:
    user_home = os.path.expanduser("~")
    tools_dir = os.path.join(user_home, ".npcsh", "npc_team", "tools")
    tools = []
    if os.path.exists(tools_dir):
        for file in os.listdir(tools_dir):
            if file.endswith(".tool"):
                with open(os.path.join(tools_dir, file), "r") as f:
                    tool_data = yaml.safe_load(f)
                    tools.append(tool_data)
    return jsonify({"tools": tools})


# except Exception as e:
#    return jsonify({"error": str(e)}), 500


@app.route("/api/tools/project", methods=["GET"])
def get_project_tools():
    current_path = request.args.get(
        "currentPath"
    )  # Correctly retrieves `currentPath` from query params
    if not current_path:
        return jsonify({"tools": []})

    if not current_path.endswith("npc_team"):
        current_path = os.path.join(current_path, "npc_team")

    tools_dir = os.path.join(current_path, "tools")
    tools = []
    if os.path.exists(tools_dir):
        for file in os.listdir(tools_dir):
            if file.endswith(".tool"):
                with open(os.path.join(tools_dir, file), "r") as f:
                    tool_data = yaml.safe_load(f)
                    tools.append(tool_data)
    return jsonify({"tools": tools})


@app.route("/api/tools/save", methods=["POST"])
def save_tool():
    try:
        data = request.json
        tool_data = data.get("tool")
        is_global = data.get("isGlobal")
        current_path = data.get("currentPath")
        tool_name = tool_data.get("tool_name")

        if not tool_name:
            return jsonify({"error": "Tool name is required"}), 400

        if is_global:
            tools_dir = os.path.join(
                os.path.expanduser("~"), ".npcsh", "npc_team", "tools"
            )
        else:
            if not current_path.endswith("npc_team"):
                current_path = os.path.join(current_path, "npc_team")
            tools_dir = os.path.join(current_path, "tools")

        os.makedirs(tools_dir, exist_ok=True)

        # Full tool structure
        tool_yaml = {
            "description": tool_data.get("description", ""),
            "inputs": tool_data.get("inputs", []),
            "steps": tool_data.get("steps", []),
        }

        file_path = os.path.join(tools_dir, f"{tool_name}.tool")
        with open(file_path, "w") as f:
            yaml.safe_dump(tool_yaml, f, sort_keys=False)

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/save_npc", methods=["POST"])
def save_npc():
    try:
        data = request.json
        npc_data = data.get("npc")
        is_global = data.get("isGlobal")
        current_path = data.get("currentPath")

        if not npc_data or "name" not in npc_data:
            return jsonify({"error": "Invalid NPC data"}), 400

        # Determine the directory based on whether it's global or project
        if is_global:
            npc_directory = os.path.expanduser("~/.npcsh/npc_team")
        else:
            npc_directory = os.path.join(current_path, "npc_team")

        # Ensure the directory exists
        os.makedirs(npc_directory, exist_ok=True)

        # Create the YAML content
        yaml_content = f"""name: {npc_data['name']}
primary_directive: "{npc_data['primary_directive']}"
model: {npc_data['model']}
provider: {npc_data['provider']}
api_url: {npc_data.get('api_url', '')}
use_global_tools: {str(npc_data.get('use_global_tools', True)).lower()}
"""

        # Save the file
        npc_file_path = os.path.join(npc_directory, f"{npc_data['name']}.npc")
        with open(npc_file_path, "w") as f:
            f.write(yaml_content)

        return jsonify({"message": "NPC saved successfully", "error": None})

    except Exception as e:
        print(f"Error saving NPC: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/npc_team_project", methods=["GET"])
def get_npc_team_project():
    try:
        db_conn = get_db_connection()

        project_npc_directory = request.args.get("currentPath")
        if not project_npc_directory.endswith("npc_team"):
            project_npc_directory = os.path.join(project_npc_directory, "npc_team")

        npc_data = []

        for npc_file in os.listdir(project_npc_directory):
            print(npc_file)
            if npc_file.endswith(".npc"):
                npc_path = os.path.join(project_npc_directory, npc_file)
                npc = load_npc_from_file(npc_path, db_conn)

                # Serialize the NPC data, including tools
                serialized_npc = {
                    "name": npc.name,
                    "primary_directive": npc.primary_directive,
                    "model": npc.model,
                    "provider": npc.provider,
                    "api_url": npc.api_url,
                    "use_global_tools": npc.use_global_tools,
                    "tools": [
                        {
                            "tool_name": tool.tool_name,
                            "inputs": tool.inputs,
                            "preprocess": tool.preprocess,
                            "prompt": tool.prompt,
                            "postprocess": tool.postprocess,
                        }
                        for tool in npc.tools
                    ],
                }
                npc_data.append(serialized_npc)

        print(npc_data)
        return jsonify({"npcs": npc_data, "error": None})

    except Exception as e:
        print(f"Error fetching NPC team: {str(e)}")
        return jsonify({"npcs": [], "error": str(e)})


@app.route("/api/get_attachment_response", methods=["POST"])
def get_attachment_response():
    data = request.json
    attachments = data.get("attachments", [])
    messages = data.get("messages")  # Get conversation ID
    conversation_id = data.get("conversationId")
    current_path = data.get("currentPath")
    command_history = CommandHistory(db_path)
    model = data.get("model")
    npc = data.get("npc")
    # load the npc properly
    # try global /porject

    # Process each attachment
    images = []
    for attachment in attachments:
        extension = attachment["name"].split(".")[-1]
        extension_mapped = extension_map.get(extension.upper(), "others")
        file_path = os.path.expanduser(
            "~/.npcsh/" + extension_mapped + "/" + attachment["name"]
        )
        if extension_mapped == "images":
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            img = Image.open(attachment["path"])
            img.save(file_path, optimize=True, quality=50)
            images.append({"filename": attachment["name"], "file_path": file_path})

    message_to_send = messages[-1]["content"][0]

    response = get_llm_response(
        message_to_send,
        images=images,
        messages=messages,
        model=model,
    )
    messages = response["messages"]
    response = response["response"]

    # Save new messages
    save_conversation_message(
        command_history, conversation_id, "user", message_to_send, wd=current_path
    )

    save_conversation_message(
        command_history,
        conversation_id,
        "assistant",
        response,
        wd=current_path,
    )
    return jsonify(
        {
            "status": "success",
            "message": response,
            "conversationId": conversation_id,
            "messages": messages,  # Optionally return fetched messages
        }
    )


@app.route("/api/execute", methods=["POST"])
def execute():
    try:
        data = request.json
        command = data.get("commandstr")
        current_path = data.get("currentPath")
        conversation_id = data.get("conversationId")
        model = data.get("model")
        print("model", model)
        npc = data.get("npc")
        print("npc", npc)
        # have to add something to actually load the npc, try project first then global , if  none proceed
        # with the command as is but notify.
        # also inthefrontend need to make it so that it wiwll just list the npcs properly.

        # Clean command
        command = command.strip().replace('"', "").replace("'", "").replace("`", "")

        if not command:
            return (
                jsonify(
                    {
                        "error": "No command provided",
                        "output": "Error: No command provided",
                    }
                ),
                400,
            )

        command_history = CommandHistory(db_path)

        # Fetch conversation history
        if conversation_id:
            conn = get_db_connection()
            cursor = conn.cursor()

            # Get all messages for this conversation in order
            cursor.execute(
                """
                SELECT role, content, timestamp
                FROM conversation_history
                WHERE conversation_id = ?
                ORDER BY timestamp ASC
            """,
                (conversation_id,),
            )

            conversation_messages = cursor.fetchall()

            # Format messages for LLM
            messages = [
                {
                    "role": msg["role"],
                    "content": msg["content"],
                }
                for msg in conversation_messages
            ]

            conn.close()
        else:
            messages = []

        # Execute command with conversation history

        result = execute_command(
            command=command,
            command_history=command_history,
            db_path=db_path,
            npc_compiler=npc_compiler,
            conversation_id=conversation_id,
            messages=messages,  # Pass the conversation history,
            model=model,
        )

        # Save new messages
        save_conversation_message(
            command_history, conversation_id, "user", command, wd=current_path
        )

        save_conversation_message(
            command_history,
            conversation_id,
            "assistant",
            result.get("output", ""),
            wd=current_path,
        )

        return jsonify(
            {
                "output": result.get("output", ""),
                "currentPath": os.getcwd(),
                "error": None,
                "messages": messages,  # Return updated messages
            }
        )

    except Exception as e:
        print(f"Error executing command: {str(e)}")
        import traceback

        traceback.print_exc()
        return (
            jsonify(
                {
                    "error": str(e),
                    "output": f"Error: {str(e)}",
                    "currentPath": data.get("currentPath", None),
                }
            ),
            500,
        )


def get_conversation_history(conversation_id):
    """Fetch all messages for a conversation in chronological order."""
    if not conversation_id:
        return []

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        query = """
            SELECT role, content, timestamp
            FROM conversation_history
            WHERE conversation_id = ?
            ORDER BY timestamp ASC
        """
        cursor.execute(query, (conversation_id,))
        messages = cursor.fetchall()

        return [
            {
                "role": msg["role"],
                "content": msg["content"],
                "timestamp": msg["timestamp"],
            }
            for msg in messages
        ]
    finally:
        conn.close()


@app.route("/api/conversations", methods=["GET"])
def get_conversations():
    try:
        path = request.args.get("path")
        if not path:
            return jsonify({"error": "No path provided", "conversations": []}), 400

        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            query = """
            SELECT DISTINCT conversation_id,
                   MIN(timestamp) as start_time,
                   GROUP_CONCAT(content) as preview
            FROM conversation_history
            WHERE directory_path = ?
            GROUP BY conversation_id
            ORDER BY start_time DESC
            """

            cursor.execute(query, [path])
            conversations = cursor.fetchall()

            return jsonify(
                {
                    "conversations": [
                        {
                            "id": conv["conversation_id"],
                            "timestamp": conv["start_time"],
                            "preview": (
                                conv["preview"][:100] + "..."
                                if conv["preview"] and len(conv["preview"]) > 100
                                else conv["preview"]
                            ),
                        }
                        for conv in conversations
                    ],
                    "error": None,
                }
            )

        finally:
            conn.close()

    except Exception as e:
        print(f"Error getting conversations: {str(e)}")
        return jsonify({"error": str(e), "conversations": []}), 500


@app.route("/api/conversation/<conversation_id>/messages", methods=["GET"])
def get_conversation_messages(conversation_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Modified query to ensure proper ordering and deduplication
        query = """
            WITH ranked_messages AS (
                SELECT
                    ch.*,
                    GROUP_CONCAT(ma.id) as attachment_ids,
                    ROW_NUMBER() OVER (
                        PARTITION BY ch.role, strftime('%s', ch.timestamp)
                        ORDER BY ch.id DESC
                    ) as rn
                FROM conversation_history ch
                LEFT JOIN message_attachments ma
                    ON ch.message_id = ma.message_id
                WHERE ch.conversation_id = ?
                GROUP BY ch.id, ch.timestamp
            )
            SELECT *
            FROM ranked_messages
            WHERE rn = 1
            ORDER BY timestamp ASC, id ASC
        """

        cursor.execute(query, [conversation_id])
        messages = cursor.fetchall()
        print(messages)

        return jsonify(
            {
                "messages": [
                    {
                        "message_id": msg["message_id"],
                        "role": msg["role"],
                        "content": msg["content"],
                        "timestamp": msg["timestamp"],
                        "model": msg["model"],
                        "provider": msg["provider"],
                        "npc": msg["npc"],
                        "attachments": (
                            get_message_attachments(msg["message_id"])
                            if msg["attachment_ids"]
                            else []
                        ),
                    }
                    for msg in messages
                ],
                "error": None,
            }
        )

    except Exception as e:
        print(f"Error getting conversation messages: {str(e)}")
        return jsonify({"error": str(e), "messages": []}), 500
    finally:
        conn.close()


@app.route("/api/stream", methods=["POST"])
def stream_raw():
    """SSE stream that takes messages, models, providers, and attachments from frontend."""
    data = request.json
    commandstr = data.get("commandstr")
    conversation_id = data.get("conversationId")
    model = data.get("model", None)
    provider = data.get("provider", None)
    save_to_sqlite3 = data.get("saveToSqlite3", False)
    npc = data.get("npc", None)
    attachments = data.get("attachments", [])
    current_path = data.get("currentPath")
    print(data)

    messages = data.get("messages", [])
    print("messages", messages)
    command_history = CommandHistory(db_path)

    images = []
    attachments_loaded = []

    if attachments:
        for attachment in attachments:
            extension = attachment["name"].split(".")[-1]
            extension_mapped = extension_map.get(extension.upper(), "others")
            file_path = os.path.expanduser(
                "~/.npcsh/" + extension_mapped + "/" + attachment["name"]
            )

            if extension_mapped == "images":
                # Open the image file and save it to the file path
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                img = Image.open(attachment["path"])

                # Save the image to a BytesIO buffer (to extract binary data)
                img_byte_arr = BytesIO()
                img.save(img_byte_arr, format="PNG")  # or the appropriate format
                img_byte_arr.seek(0)  # Rewind the buffer to the beginning

                # Save the image to a file
                img.save(file_path, optimize=True, quality=50)

                # Add to images list for LLM processing
                images.append({"filename": attachment["name"], "file_path": file_path})

                # Add the image data (in binary form) to attachments_loaded
                attachments_loaded.append(
                    {
                        "name": attachment["name"],
                        "type": extension_mapped,
                        "data": img_byte_arr.read(),  # Read binary data from the buffer
                        "size": os.path.getsize(file_path),
                    }
                )
    if save_to_sqlite3:
        if len(messages) == 0:
            # load the conversation messages
            messages = fetch_messages_for_conversation(conversation_id)
        if not messages:
            return jsonify({"error": "No messages provided"}), 400
        messages.append({"role": "user", "content": commandstr})
        message_id = command_history.generate_message_id()

        save_conversation_message(
            command_history,
            conversation_id,
            "user",
            commandstr,
            wd=current_path,
            model=model,
            provider=provider,
            npc=npc,
            attachments=attachments_loaded,
            message_id=message_id,
        )
        message_id = command_history.generate_message_id()

    stream_response = get_stream(
        messages,
        images=images,
        model=model,
        provider=provider,
        npc=npc if isinstance(npc, NPC) else None,
    )

    """else:

        stream_response = execute_command_stream(
            commandstr,
            command_history,
            db_path,
            npc_compiler,
            model=model,
            provider=provider,
            messages=messages,
            images=images,  # Pass the processed images
        )  # Get all conversation messages so far
    """
    final_response = ""  # To accumulate the assistant's response
    complete_response = []  # List to store all chunks

    def event_stream():
        for response_chunk in stream_response:
            chunk_content = ""

            chunk_content = "".join(
                choice.delta.content
                for choice in response_chunk.choices
                if choice.delta.content is not None
            )
            if chunk_content:
                complete_response.append(chunk_content)
            chunk_data = {
                "type": "content",  # Added type
                "id": response_chunk.id,
                "object": response_chunk.object,
                "created": response_chunk.created,
                "model": response_chunk.model,
                "choices": [
                    {
                        "index": choice.index,
                        "delta": {
                            "content": choice.delta.content,
                            "role": choice.delta.role,
                        },
                        "finish_reason": choice.finish_reason,
                    }
                    for choice in response_chunk.choices
                ],
            }
            yield f"{json.dumps(chunk_data)}\n\n"

            if save_to_sqlite3:
                save_conversation_message(
                    command_history,
                    conversation_id,
                    "assistant",
                    chunk_content,
                    wd=current_path,
                    model=model,
                    provider=provider,
                    npc=npc,
                    message_id=message_id,  # Save with the same message_id
                )

            # Send completion message
            yield f"{json.dumps({'type': 'message_stop'})}\n\n"
            if save_to_sqlite3:
                full_content = command_history.get_full_message_content(message_id)
                command_history.update_message_content(message_id, full_content)

        response = Response(event_stream(), mimetype="text/event-stream")

        return response

    response = Response(event_stream(), mimetype="text/event-stream")

    return response


@app.after_request
def after_request(response):
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
    response.headers.add("Access-Control-Allow-Credentials", "true")
    return response


def get_db_connection():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


extension_map = {
    "PNG": "images",
    "JPG": "images",
    "JPEG": "images",
    "GIF": "images",
    "SVG": "images",
    "MP4": "videos",
    "AVI": "videos",
    "MOV": "videos",
    "WMV": "videos",
    "MPG": "videos",
    "MPEG": "videos",
    "DOC": "documents",
    "DOCX": "documents",
    "PDF": "documents",
    "PPT": "documents",
    "PPTX": "documents",
    "XLS": "documents",
    "XLSX": "documents",
    "TXT": "documents",
    "CSV": "documents",
    "ZIP": "archives",
    "RAR": "archives",
    "7Z": "archives",
    "TAR": "archives",
    "GZ": "archives",
    "BZ2": "archives",
    "ISO": "archives",
}


def fetch_messages_for_conversation(conversation_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
        SELECT role, content, timestamp
        FROM conversation_history
        WHERE conversation_id = ?
        ORDER BY timestamp ASC
    """
    cursor.execute(query, (conversation_id,))
    messages = cursor.fetchall()
    conn.close()

    return [
        {
            "role": message["role"],
            "content": message["content"],
            "timestamp": message["timestamp"],
        }
        for message in messages
    ]


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "error": None})


def start_flask_server(
    port=5337,
    cors_origins=None,
):
    try:
        # Ensure the database tables exist
        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            # Create tables if they don't exist
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS command_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    command TEXT,
                    tags TEXT,
                    response TEXT,
                    directory TEXT,
                    conversation_id TEXT
                )
            """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    role TEXT,
                    content TEXT,
                    conversation_id TEXT,
                    directory_path TEXT
                )
            """
            )

            conn.commit()
        finally:
            conn.close()

        # Only apply CORS if origins are specified
        if cors_origins:
            from flask_cors import CORS

            CORS(
                app,
                origins=cors_origins,
                allow_headers=["Content-Type", "Authorization"],
                methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                supports_credentials=True,
            )

        # Run the Flask app on all interfaces
        print("Starting Flask server on http://0.0.0.0:5337")
        app.run(host="0.0.0.0", port=5337, debug=True)
    except Exception as e:
        print(f"Error starting server: {str(e)}")


if __name__ == "__main__":
    start_flask_server()
