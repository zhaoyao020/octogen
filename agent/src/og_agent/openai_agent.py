# Copyright (C) 2023 dbpunk.com Author imotai <codego.me@gmail.com>
# SPDX-FileCopyrightText: 2023 imotai <jackwang@octogen.dev>
# SPDX-FileContributor: imotai
#
# SPDX-License-Identifier: Elastic-2.0

""" """
import openai
import io
import json
import logging
import time
from pydantic import BaseModel, Field
from og_proto.agent_server_pb2 import OnAgentAction, TaskRespond, OnAgentActionEnd, FinalRespond
from .base_agent import BaseAgent, TypingState, TaskContext
from .tokenizer import tokenize
import tiktoken

logger = logging.getLogger(__name__)
encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")
OCTOGEN_FUNCTIONS = [
    {
        "name": "execute_python_code",
        "description": "Safely execute arbitrary Python code and return the result, stdout, and stderr.",
        "parameters": {
            "type": "object",
            "properties": {
                "explanation": {
                    "type": "string",
                    "description": "the explanation about the python code",
                },
                "code": {
                    "type": "string",
                    "description": "the python code to be executed",
                },
                "saved_filenames": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "A list of filenames that were created by the code",
                },
            },
            "required": ["explanation", "code"],
        },
    },
]


class OpenaiAgent(BaseAgent):

    def __init__(self, model, system_prompt, sdk, is_azure=True):
        super().__init__(sdk)
        self.model = model
        self.system_prompt = system_prompt
        logger.info(f"use openai model {model} is_azure {is_azure}")
        logger.info(f"use openai with system prompt {system_prompt}")
        self.is_azure = is_azure

    def _merge_delta_for_function_call(self, message, delta):
        if not delta:
            return
        if "function_call" not in message:
            message["function_call"] = delta["function_call"]
            return
        old_arguments = message["function_call"].get("arguments", "")
        if delta["function_call"]["arguments"]:
            message["function_call"]["arguments"] = (
                old_arguments + delta["function_call"]["arguments"]
            )

    def _merge_delta_for_content(self, message, delta):
        if not delta:
            return
        content = message.get("content", "")
        if delta.get("content"):
            message["content"] = content + delta["content"]

    def _get_function_call_argument_new_typing(self, message):
        if message["function_call"]["name"] == "python":
            return TypingState.CODE, "", message["function_call"].get("arguments", "")

        arguments = message["function_call"].get("arguments", "")
        state = TypingState.START
        explanation_str = ""
        code_str = ""
        for token_state, token in tokenize(io.StringIO(arguments)):
            if token_state == None:
                if state == TypingState.EXPLANATION and token[0] == 1:
                    explanation_str = token[1]
                    state = TypingState.START
                if state == TypingState.CODE and token[0] == 1:
                    code_str = token[1]
                    state = TypingState.START
                if token[1] == "explanation":
                    state = TypingState.EXPLANATION
                if token[1] == "code":
                    state = TypingState.CODE
            else:
                # String
                if token_state == 9 and state == TypingState.EXPLANATION:
                    explanation_str = "".join(token)
                elif token_state == 9 and state == TypingState.CODE:
                    code_str = "".join(token)
        return (state, explanation_str, code_str)

    async def call_openai(self, messages, queue, context, task_context):
        """
        call the openai api
        """
        sent_token_count = 0
        for message in messages:
            if not message["content"]:
                continue
            sent_token_count += len(encoding.encode(message["content"]))
        task_context.sent_token_count += sent_token_count

        start_time = time.time()
        if self.is_azure:
            response = await openai.ChatCompletion.acreate(
                engine=self.model,
                messages=messages,
                temperature=0,
                functions=OCTOGEN_FUNCTIONS,
                function_call="auto",
                stream=True,
            )
        else:
            response = await openai.ChatCompletion.acreate(
                model=self.model,
                messages=messages,
                temperature=0,
                functions=OCTOGEN_FUNCTIONS,
                function_call="auto",
                stream=True,
            )
        message = None
        text_content = ""
        code_content = ""
        async for chunk in response:
            if context.done():
                logger.debug("the client has cancelled the request")
                break
            if not chunk["choices"]:
                continue

            delta = chunk["choices"][0]["delta"]
            logger.debug(f"{delta}")
            if not message:
                message = delta
            else:
                if "function_call" in delta:
                    self._merge_delta_for_function_call(message, delta)
                    arguments = message["function_call"].get("arguments", "")
                    task_context.generated_token_count += len(
                        encoding.encode(arguments)
                    )
                    task_context.model_respond_duration += int(
                        (time.time() - start_time) * 1000
                    )
                    start_time = time.time()
                    (
                        state,
                        explanation_str,
                        code_str,
                    ) = self._get_function_call_argument_new_typing(message)
                    if explanation_str and text_content != explanation_str:
                        typed_chars = explanation_str[len(text_content) :]
                        text_content = explanation_str
                        await queue.put(
                            TaskRespond(
                                state=task_context.to_task_state_proto(),
                                respond_type=TaskRespond.OnAgentTextTyping,
                                typing_content=typed_chars,
                            )
                        )
                    if code_str and code_content != code_str:
                        typed_chars = code_str[len(code_content) :]
                        code_content = code_str
                        await queue.put(
                            TaskRespond(
                                state=task_context.to_task_state_proto(),
                                respond_type=TaskRespond.OnAgentCodeTyping,
                                typing_content=typed_chars,
                            )
                        )
                    logger.debug(
                        f"argument explanation:{explanation_str} code:{code_str}"
                    )
                else:
                    self._merge_delta_for_content(message, delta)
                    task_context.model_respond_duration += int(
                        (time.time() - start_time) * 1000
                    )
                    start_time = time.time()
                    if delta.get("content") != None:
                        task_context.generated_token_count += len(
                            encoding.encode(delta.get("content"))
                        )
                        await queue.put(
                            TaskRespond(
                                state=task_context.to_task_state_proto(),
                                respond_type=TaskRespond.OnAgentTextTyping,
                                typing_content=delta["content"],
                            )
                        )
        return message

    async def handle_function(self, message, queue, context, task_context):
        if "function_call" in message:
            if context.done():
                logging.debug("the client has cancelled the request")
                return
            function_name = message["function_call"]["name"]
            code = ""
            explanation = ""
            saved_filenames = []
            if function_name == "python":
                code = message["function_call"]["arguments"]
                logger.debug(f"call function {function_name} with args {code}")
            else:
                arguments = json.loads(message["function_call"]["arguments"])
                logger.debug(f"call function {function_name} with args {arguments}")
                code = arguments["code"]
                explanation = arguments["explanation"]
                saved_filenames = arguments.get("saved_filenames", [])
            tool_input = json.dumps({
                "code": code,
                "explanation": explanation,
                "saved_filenames": saved_filenames,
            })
            # send the respond to client
            await queue.put(
                TaskRespond(
                    state=task_context.to_task_state_proto(),
                    respond_type=TaskRespond.OnAgentActionType,
                    on_agent_action=OnAgentAction(
                        input=tool_input, tool="execute_python_code"
                    ),
                )
            )
            function_result = None
            async for (result, respond) in self.call_function(
                code, context, task_context
            ):
                if context.done():
                    logger.debug("the client has cancelled the request")
                    break
                function_result = result
                if respond:
                    await queue.put(respond)
            return function_result
        else:
            raise Exception("bad message, function message expected")

    async def arun(self, task, queue, context, max_iteration=5):
        """ """
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        iterations = 0
        task_context = TaskContext(
            start_time=time.time(),
            generated_token_count=0,
            sent_token_count=0,
            model_name="openai",
            iteration_count=0,
            model_respond_duration=0,
        )
        try:
            while iterations < max_iteration and not context.cancelled():
                iterations += 1
                task_context.iteration_count = iterations
                logger.debug(f" the input messages {messages}")
                chat_message = await self.call_openai(
                    messages, queue, context, task_context
                )
                logger.debug(f"the response {chat_message}")
                if "function_call" in chat_message:
                    if "content" not in chat_message:
                        chat_message["content"] = None
                    messages.append(chat_message)
                    function_name = chat_message["function_call"]["name"]
                    if function_name not in ["execute_python_code", "python"]:
                        messages.append({
                            "role": "function",
                            "name": "execute_python_code",
                            "content": "You can use the execute_python_code only",
                        })
                        continue
                    function_result = await self.handle_function(
                        chat_message, queue, context, task_context
                    )
                    await queue.put(
                        TaskRespond(
                            state=task_context.to_task_state_proto(),
                            respond_type=TaskRespond.OnAgentActionEndType,
                            on_agent_action_end=OnAgentActionEnd(
                                output="",
                                output_files=function_result.saved_filenames,
                                has_error=function_result.has_error,
                            ),
                        )
                    )
                    # TODO optimize the token limitation
                    if function_result.has_result:
                        messages.append({
                            "role": "function",
                            "name": "execute_python_code",
                            "content": function_result.console_stdout[0:500],
                        })
                    elif function_result.has_error:
                        messages.append({
                            "role": "function",
                            "name": "execute_python_code",
                            "content": function_result.console_stderr[0:500],
                        })
                    else:
                        messages.append({
                            "role": "function",
                            "name": "execute_python_code",
                            "content": function_result.console_stdout[0:500],
                        })
                else:
                    # end task
                    await queue.put(
                        TaskRespond(
                            state=task_context.to_task_state_proto(),
                            respond_type=TaskRespond.OnFinalAnswerType,
                            final_respond=FinalRespond(answer=chat_message["content"]),
                        )
                    )
                    break
        finally:
            await queue.put(None)
