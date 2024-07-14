from typing import Optional, List, Dict, Generator
import logging
from dataclasses import dataclass
import json

from kaizen.helpers import output, parser
from kaizen.llms.provider import LLMProvider
from kaizen.llms.prompts.code_review_prompts import (
    PR_DESCRIPTION_PROMPT,
    MERGE_PR_DESCRIPTION_PROMPT,
    PR_FILE_DESCRIPTION_PROMPT,
    PR_DESC_EVALUATION_PROMPT,
    CODE_REVIEW_SYSTEM_PROMPT,
)


@dataclass
class DescOutput:
    desc: str
    usage: Dict[str, int]
    model_name: str
    cost: Dict[str, float]


class PRDescriptionGenerator:
    def __init__(self, llm_provider: LLMProvider):
        self.logger = logging.getLogger(__name__)
        self.provider = llm_provider
        self.provider.system_prompt = CODE_REVIEW_SYSTEM_PROMPT
        self.total_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def generate_pull_request_desc(
        self,
        diff_text: str,
        pull_request_title: str,
        pull_request_desc: str,
        pull_request_files: List[Dict],
        user: Optional[str] = None,
        reeval_response: bool = False,
    ) -> DescOutput:
        prompt = PR_DESCRIPTION_PROMPT.format(
            PULL_REQUEST_TITLE=pull_request_title,
            PULL_REQUEST_DESC=pull_request_desc,
            CODE_DIFF=diff_text,
        )
        if not diff_text and not pull_request_files:
            raise Exception("Both diff_text and pull_request_files are empty!")

        if diff_text and self.provider.is_inside_token_limit(PROMPT=prompt):
            desc = self._process_full_diff(prompt, user, reeval_response)
        else:
            desc = self._process_files(
                pull_request_files,
                pull_request_title,
                pull_request_desc,
                user,
                reeval_response,
            )

        body = output.create_pr_description(desc, pull_request_desc)
        prompt_cost, completion_cost = self.provider.get_usage_cost(
            total_usage=self.total_usage
        )

        return DescOutput(
            desc=body,
            usage=self.total_usage,
            model_name=self.provider.model,
            cost={"prompt_cost": prompt_cost, "completion_cost": completion_cost},
        )

    def _process_full_diff(
        self,
        prompt: str,
        user: Optional[str],
        reeval_response: bool,
    ) -> str:
        self.logger.debug("Processing directly from diff")
        resp, usage = self.provider.chat_completion_with_format(prompt, user=user, format="markdown")
        self.total_usage = self.provider.update_usage(self.total_usage, usage)

        if reeval_response:
            resp = self._reevaluate_response(prompt, resp, user)
        return resp

    def _process_files(
        self,
        pull_request_files: List[Dict],
        pull_request_title: str,
        pull_request_desc: str,
        user: Optional[str],
        reeval_response: bool,
    ) -> List[Dict]:
        self.logger.debug("Processing based on files")
        file_descs = []
        for file_review in self._process_files_generator(
            pull_request_files,
            pull_request_title,
            pull_request_desc,
            user,
            reeval_response,
        ):
            file_descs.extend(file_review)

        prompt = MERGE_PR_DESCRIPTION_PROMPT.format(DESCS=json.dumps(file_descs))
        resp, usage = self.provider.chat_completion_with_format(prompt, user=user, format="markdown")
        self.total_usage = self.provider.update_usage(self.total_usage, usage)

        return resp

    def _process_files_generator(
        self,
        pull_request_files: List[Dict],
        pull_request_title: str,
        pull_request_desc: str,
        user: Optional[str],
        reeval_response: bool,
    ) -> Generator[List[Dict], None, None]:
        combined_diff_data = ""
        available_tokens = self.provider.available_tokens(
            PR_FILE_DESCRIPTION_PROMPT.format(
                PULL_REQUEST_TITLE=pull_request_title,
                PULL_REQUEST_DESC=pull_request_desc,
                CODE_DIFF="",
            )
        )

        for file in pull_request_files:
            patch_details = file.get("patch")
            filename = file.get("filename", "")

            if (
                filename.split(".")[-1] not in parser.EXCLUDED_FILETYPES
                and patch_details is not None
            ):
                temp_prompt = (
                    combined_diff_data
                    + f"\n---->\nFile Name: {filename}\nPatch Details: \n{patch_details}"
                )

                if available_tokens - self.provider.get_token_count(temp_prompt) > 0:
                    combined_diff_data = temp_prompt
                    continue

                yield self._process_file_chunk(
                    combined_diff_data,
                    pull_request_title,
                    pull_request_desc,
                    user,
                    reeval_response,
                )
                combined_diff_data = (
                    f"\n---->\nFile Name: {filename}\nPatch Details: {patch_details}"
                )

        if combined_diff_data:
            yield self._process_file_chunk(
                combined_diff_data,
                pull_request_title,
                pull_request_desc,
                user,
                reeval_response,
            )

    def _process_file_chunk(
        self,
        diff_data: str,
        pull_request_title: str,
        pull_request_desc: str,
        user: Optional[str],
        reeval_response: bool,
    ) -> List[Dict]:
        prompt = PR_FILE_DESCRIPTION_PROMPT.format(
            PULL_REQUEST_TITLE=pull_request_title,
            PULL_REQUEST_DESC=pull_request_desc,
            CODE_DIFF=diff_data,
        )
        resp, usage = self.provider.chat_completion_with_format(prompt, user=user, format="markdown")
        self.total_usage = self.provider.update_usage(self.total_usage, usage)

        if reeval_response:
            resp = self._reevaluate_response(prompt, resp, user)

        return resp

    def _reevaluate_response(self, prompt: str, resp: str, user: Optional[str]) -> str:
        messages = [
            {"role": "system", "content": self.provider.system_prompt},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": resp},
            {"role": "user", "content": PR_DESC_EVALUATION_PROMPT},
        ]
        resp, usage = self.provider.chat_completion(
            prompt, user=user, messages=messages
        )
        self.total_usage = self.provider.update_usage(self.total_usage, usage)
        return resp
