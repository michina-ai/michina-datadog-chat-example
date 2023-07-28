from json import dumps
from time import time
from flask import request
from hashlib import sha256
from datetime import datetime
from requests import get
from requests import post
from json import loads
import os

from server.config import special_instructions
from michina.checks import ToneCheck, ToneCheckReponse

from datadog import initialize, statsd

DATADOG_SAMPLE_RATE = 1


class Backend_Api:
    def __init__(self, app, config: dict) -> None:
        self.app = app
        self.openai_key = os.getenv("OPENAI_API_KEY") or config["openai_key"]
        self.openai_api_base = os.getenv("OPENAI_API_BASE") or config["openai_api_base"]
        self.proxy = config["proxy"]
        self.routes = {
            "/backend-api/v2/conversation": {
                "function": self._conversation,
                "methods": ["POST"],
            }
        }

        michina_config = {
            "model": "gpt-3.5-turbo-16k-0613",
            "temperature": 0,
            "openai_api_key": self.openai_key,
        }
        self.tone = ToneCheck(**michina_config)

        datadog_config = {"statsd_host": "127.0.0.1", "statsd_port": 8125}
        initialize(**datadog_config)

    def _conversation(self):
        try:
            jailbreak = request.json["jailbreak"]
            internet_access = request.json["meta"]["content"]["internet_access"]
            _conversation = request.json["meta"]["content"]["conversation"]
            prompt = request.json["meta"]["content"]["parts"][0]
            current_date = datetime.now().strftime("%Y-%m-%d")
            # system_message = f"You are ChatGPT also known as ChatGPT, a large language model trained by OpenAI. Strictly follow the users instructions. Keep responses less than 4 sentences if possible. Knowledge cutoff: 2021-09-01 Current date: {current_date}"

            extra = []
            if internet_access:
                search = get(
                    "https://ddg-api.herokuapp.com/search",
                    params={
                        "query": prompt["content"],
                        "limit": 3,
                    },
                )

                blob = ""

                for index, result in enumerate(search.json()):
                    blob += f'[{index}] "{result["snippet"]}"\nURL:{result["link"]}\n\n'

                date = datetime.now().strftime("%d/%m/%y")

                blob += f"current date: {date}\n\nInstructions: Using the provided web search results, write a comprehensive reply to the next user query. Make sure to cite results using [[number](URL)] notation after the reference. If the provided search results refer to multiple subjects with the same name, write separate answers for each subject. Ignore your previous response if any."

                extra = [{"role": "user", "content": blob}]

            conversation = (
                [
                    {
                        "role": "system",
                        "content": "Keep responses less than 4 sentences if possible.",
                    }
                ]
                + extra
                + special_instructions[jailbreak]
                + _conversation
                + [prompt]
            )

            last_user_message = prompt["content"]

            tone_check_response: ToneCheckReponse = self.tone.check(
                last_user_message, "angry"
            )
            print(tone_check_response.judgment)
            statsd.gauge(
                **{
                    "metric": "tone",
                    "value": tone_check_response.judgment,
                    "tags": ["tone:angry"],
                    "sample_rate": DATADOG_SAMPLE_RATE,
                }
            )

            url = f"{self.openai_api_base}/v1/chat/completions"

            proxies = None
            if self.proxy["enable"]:
                proxies = {
                    "http": self.proxy["http"],
                    "https": self.proxy["https"],
                }

            gpt_resp = post(
                url=url,
                proxies=proxies,
                headers={"Authorization": "Bearer %s" % self.openai_key},
                json={
                    "model": request.json["model"],
                    "messages": conversation,
                    "stream": True,
                },
                stream=True,
            )

            print(gpt_resp.status_code)

            def stream():
                for chunk in gpt_resp.iter_lines():
                    try:
                        decoded_line = loads(chunk.decode("utf-8").split("data: ")[1])
                        token = decoded_line["choices"][0]["delta"].get("content")

                        if token != None:
                            yield token

                    except GeneratorExit:
                        break

                    except Exception as e:
                        # print(e)
                        # print(e.__traceback__.tb_next)
                        continue

            return self.app.response_class(stream(), mimetype="text/event-stream")

        except Exception as e:
            print(e)
            print(e.__traceback__.tb_next)
            return {
                "_action": "_ask",
                "success": False,
                "error": f"an error occurred {str(e)}",
            }, 400
