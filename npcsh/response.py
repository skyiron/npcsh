from typing import Any, Dict, Generator, List, Union
from pydantic import BaseModel
import os
import anthropic
import ollama  # Add to setup.py if missing
from openai import OpenAI
from diffusers import StableDiffusionPipeline
from google.generativeai import types
import google.generativeai as genai
from .npc_sysenv import (
    get_system_message,
    compress_image,
    available_chat_models,
    available_reasoning_models,
)

import json
import requests
import base64
from PIL import Image


def get_deepseek_response(
    prompt: str,
    model: str,
    images: List[Dict[str, str]] = None,
    npc: Any = None,
    format: Union[str, BaseModel] = None,
    messages: List[Dict[str, str]] = None,
    api_key: str = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Function Description:
        This function generates a response using the DeepSeek API.
    Args:
        prompt (str): The prompt for generating the response.
        model (str): The model to use for generating the response.
    Keyword Args:
        images (List[Dict[str, str]]): The list of images.
        npc (Any): The NPC object.
        format (str): The format of the response.
        messages (List[Dict[str, str]]): The list of messages.
    Returns:
        Any: The response generated by the DeepSeek API.


    """
    if api_key is None:
        api_key = os.getenv("DEEPSEEK_API_KEY", None)
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    print(client)

    system_message = get_system_message(npc) if npc else "You are a helpful assistant."
    if messages is None or len(messages) == 0:
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ]
    if images:
        for image in images:
            # print(f"Image file exists: {os.path.exists(image['file_path'])}")

            with open(image["file_path"], "rb") as image_file:
                image_data = base64.b64encode(compress_image(image_file.read())).decode(
                    "utf-8"
                )
                messages[-1]["content"].append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}",
                        },
                    }
                )
    print(messages)
    # print(model)
    response_format = None if format == "json" else format
    if response_format is None:
        completion = client.chat.completions.create(model=model, messages=messages)
        llm_response = completion.choices[0].message.content
        items_to_return = {"response": llm_response}

        items_to_return["messages"] = messages
        # print(llm_response, model)
        if format == "json":
            try:
                items_to_return["response"] = json.loads(llm_response)

                return items_to_return
            except json.JSONDecodeError:
                print(f"Warning: Expected JSON response, but received: {llm_response}")
                return {"error": "Invalid JSON response"}
        else:
            items_to_return["messages"].append(
                {"role": "assistant", "content": llm_response}
            )
            return items_to_return

    else:
        if model in available_reasoning_models:
            raise NotImplementedError("Reasoning models do not support JSON output.")
        try:
            completion = client.beta.chat.completions.parse(
                model=model, messages=messages, response_format=response_format
            )
            items_to_return = {"response": completion.choices[0].message.parsed.dict()}
            items_to_return["messages"] = messages

            items_to_return["messages"].append(
                {"role": "assistant", "content": completion.choices[0].message.parsed}
            )
            return items_to_return
        except Exception as e:
            print("pydantic outputs not yet implemented with deepseek?")


def get_ollama_response(
    prompt: str,
    model: str,
    images: List[Dict[str, str]] = None,
    npc: Any = None,
    format: Union[str, BaseModel] = None,
    messages: List[Dict[str, str]] = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Generates a response using the Ollama API.

    Args:
        prompt (str): Prompt for generating the response.
        model (str): Model to use for generating the response.
        images (List[Dict[str, str]], optional): List of image data. Defaults to None.
        npc (Any, optional): Optional NPC object. Defaults to None.
        format (Union[str, BaseModel], optional): Response format or schema. Defaults to None.
        messages (List[Dict[str, str]], optional): Existing messages to append responses. Defaults to None.

    Returns:
        Dict[str, Any]: The response, optionally including updated messages.
    """
    # try:
    # Prepare the message payload

    message = {"role": "user", "content": prompt}
    if images:
        message["images"] = [image["file_path"] for image in images]

    # Prepare format
    if isinstance(format, type):
        schema = format.model_json_schema()
        res = ollama.chat(model=model, messages=[message], format=schema)

    elif isinstance(format, str):
        if format == "json":
            res = ollama.chat(model=model, messages=[message], format=format)
        else:
            res = ollama.chat(model=model, messages=[message])
    else:
        res = ollama.chat(model=model, messages=[message])
    response_content = res.get("message", {}).get("content")

    # Prepare the return dictionary
    result = {"response": response_content}

    # Append response to messages if provided
    if messages is not None:
        messages.append({"role": "assistant", "content": response_content})
        result["messages"] = messages

    # Handle JSON format if specified
    if format == "json":
        if model in available_reasoning_models:
            raise NotImplementedError("Reasoning models do not support JSON output.")
        try:
            result["response"] = json.loads(response_content)
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON response: {response_content}"}

    return result

    # except Exception as e:
    #    return {"error": f"Exception occurred: {e}"}


def get_openai_response(
    prompt: str,
    model: str,
    images: List[Dict[str, str]] = None,
    npc: Any = None,
    format: Union[str, BaseModel] = None,
    api_key: str = None,
    messages: List[Dict[str, str]] = None,
):
    """
    Function Description:
        This function generates a response using the OpenAI API.
    Args:
        prompt (str): The prompt for generating the response.
        model (str): The model to use for generating the response.
    Keyword Args:
        images (List[Dict[str, str]]): The list of images.
        npc (Any): The NPC object.
        format (str): The format of the response.
        api_key (str): The API key for accessing the OpenAI API.
        messages (List[Dict[str, str]]): The list of messages.
    Returns:
        Any: The response generated by the OpenAI API.
    """

    # try:
    if api_key is None:
        api_key = os.environ["OPENAI_API_KEY"]
    client = OpenAI(api_key=api_key)
    # print(npc)

    system_message = get_system_message(npc) if npc else "You are a helpful assistant."
    if messages is None or len(messages) == 0:
        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": [{"type": "text", "text": prompt}]},
        ]
    if images:
        for image in images:
            # print(f"Image file exists: {os.path.exists(image['file_path'])}")

            with open(image["file_path"], "rb") as image_file:
                image_data = base64.b64encode(compress_image(image_file.read())).decode(
                    "utf-8"
                )
                messages[-1]["content"].append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}",
                        },
                    }
                )
    # print(model)
    response_format = None if format == "json" else format
    if response_format is None:
        completion = client.chat.completions.create(model=model, messages=messages)
        llm_response = completion.choices[0].message.content
        items_to_return = {"response": llm_response}

        items_to_return["messages"] = messages
        # print(llm_response, model)
        if format == "json":
            if model in available_reasoning_models:
                raise NotImplementedError(
                    "Reasoning models do not support JSON output."
                )
            try:
                items_to_return["response"] = json.loads(llm_response)

                return items_to_return
            except json.JSONDecodeError:
                print(f"Warning: Expected JSON response, but received: {llm_response}")
                return {"error": "Invalid JSON response"}
        else:
            items_to_return["messages"].append(
                {"role": "assistant", "content": llm_response}
            )
            return items_to_return

    else:
        completion = client.beta.chat.completions.parse(
            model=model, messages=messages, response_format=response_format
        )
        items_to_return = {"response": completion.choices[0].message.parsed.dict()}
        items_to_return["messages"] = messages

        items_to_return["messages"].append(
            {"role": "assistant", "content": completion.choices[0].message.parsed}
        )
        return items_to_return
    # except Exception as e:
    #    print("openai api key", api_key)
    #    print(f"Error interacting with OpenAI: {e}")
    #    return f"Error interacting with OpenAI: {e}"


def get_anthropic_response(
    prompt: str,
    model: str,
    images: List[Dict[str, str]] = None,
    npc: Any = None,
    format: str = None,
    api_key: str = None,
    messages: List[Dict[str, str]] = None,
    **kwargs,
):
    """
    Function Description:
        This function generates a response using the Anthropic API.
    Args:
        prompt (str): The prompt for generating the response.
        model (str): The model to use for generating the response.
    Keyword Args:
        images (List[Dict[str, str]]): The list of images.
        npc (Any): The NPC object.
        format (str): The format of the response.
        api_key (str): The API key for accessing the Anthropic API.
        messages (List[Dict[str, str]]): The list of messages.
    Returns:
        Any: The response generated by the Anthropic API.
    """

    try:
        if api_key is None:
            api_key = os.getenv("ANTHROPIC_API_KEY", None)

        client = anthropic.Anthropic()

        # Prepare the message content
        message_content = []

        # Add images if provided
        if images:
            for img in images:
                # load image and base 64 encode
                with open(img["file_path"], "rb") as image_file:
                    img["data"] = base64.b64encode(
                        compress_image(image_file.read())
                    ).decode("utf-8")
                    img["media_type"] = "image/jpeg"
                    message_content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img["media_type"],
                                "data": img["data"],
                            },
                        }
                    )

        # Add the text prompt
        message_content.append({"type": "text", "text": prompt})

        # Create the message
        message = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": message_content}],
        )
        # print(message)

        llm_response = message.content[0].text
        items_to_return = {"response": llm_response}

        # print(format)
        # Update messages if they were provided
        if messages is None:
            messages = []
            messages.append(
                {"role": "system", "content": "You are a helpful assistant."}
            )
            messages.append({"role": "user", "content": message_content})
        items_to_return["messages"] = messages

        # Handle JSON format if requested
        if format == "json":
            try:
                items_to_return["response"] = json.loads(llm_response)
                return items_to_return
            except json.JSONDecodeError:
                print(f"Warning: Expected JSON response, but received: {llm_response}")
                return {"response": llm_response, "error": "Invalid JSON response"}
        else:
            # only append to messages if the response is not json
            messages.append({"role": "assistant", "content": llm_response})
        # print("teststea")
        return items_to_return

    except Exception as e:
        return f"Error interacting with Anthropic llm response: {e}"


def get_openai_like_response(
    prompt: str,
    model: str,
    api_url: str,
    api_key: str,
    **kwargs,
) -> Dict[str, Any]:
    try:
        if api_url is None:
            raise ValueError("api_url is required for openai-like provider")
        request_data = {
            "model": model,
            "prompt": prompt,
            **kwargs,
        }
        headers = {"Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {api_key}"
        response = requests.post(api_url, headers=headers, json=request_data)
        response.raise_for_status()
        response_json = response.json()
        return response_json
    except requests.exceptions.RequestException as e:
        return f"Error making API request: {e}"
    except Exception as e:
        return f"Error interacting with API: {e}"


def get_gemini_response(
    prompt: str,
    model: str,
    images: List[Dict[str, str]] = None,
    npc: Any = None,
    format: Union[str, BaseModel] = None,
    messages: List[Dict[str, str]] = None,
    api_key: str = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    Generates a response using the Gemini API.
    """
    # Configure the Gemini API
    if api_key is None:
        genai.configure(api_key=gemini_api_key)

    # Prepare the system message
    system_message = get_system_message(npc) if npc else "You are a helpful assistant."
    model = genai.GenerativeModel(model, system_instruction=system_message)

    # Extract just the content to send to the model
    if messages is None or len(messages) == 0:
        content_to_send = prompt
    else:
        # Get the latest message's content
        latest_message = messages[-1]
        content_to_send = (
            latest_message["parts"][0]
            if "parts" in latest_message
            else latest_message.get("content", prompt)
        )
    history = []
    if messages:
        for msg in messages:
            if "content" in msg:
                # Convert content to parts format
                history.append({"role": msg["role"], "parts": [msg["content"]]})
            else:
                # Already in parts format
                history.append(msg)
    # If no history, create a new message list
    if not history:
        history = [{"role": "user", "parts": [prompt]}]
    elif isinstance(prompt, str):  # Add new prompt to existing history
        history.append({"role": "user", "parts": [prompt]})

    # Handle images if provided
    # Handle images by adding them to the last message's parts
    if images:
        for image in images:
            with open(image["file_path"], "rb") as image_file:
                img = Image.open(image_file)
                history[-1]["parts"].append(img)
    # Generate the response
    # try:
    # Send the entire conversation history to maintain context
    response = model.generate_content(history)
    llm_response = response.text

    # Filter out empty parts
    if isinstance(llm_response, list):
        llm_response = " ".join([part for part in llm_response if part.strip()])
    elif not llm_response.strip():
        llm_response = ""

    # Prepare the return dictionary
    items_to_return = {"response": llm_response, "messages": history}
    # print(llm_response, type(llm_response))

    # Handle JSON format if specified
    if format == "json":
        if type(llm_response) == str:
            if llm_response.startswith("```json"):
                llm_response = (
                    llm_response.replace("```json", "").replace("```", "").strip()
                )
        try:
            items_to_return["response"] = json.loads(llm_response)
        except json.JSONDecodeError:
            print(f"Warning: Expected JSON response, but received: {llm_response}")
            return {"error": "Invalid JSON response"}
    else:
        # Append the model's response to the messages
        history.append({"role": "model", "parts": [llm_response]})
        items_to_return["messages"] = history

    return items_to_return

    # except Exception as e:
    #    return {"error": f"Error generating response: {str(e)}"}
