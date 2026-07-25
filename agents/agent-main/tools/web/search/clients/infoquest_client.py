"""InfoQuest 搜索与网页抓取客户端。

接入说明：
https://docs.byteplus.com/en/docs/InfoQuest/What_is_Info_Quest
"""

import json
import logging
import os
from typing import Any

import requests
from config.settings import get_config

logger = logging.getLogger(__name__)

# 从配置读取 InfoQuest URL
config = get_config()
INFOQUEST_SEARCH_URL = config.infoquest.search_url
INFOQUEST_READER_URL = config.infoquest.reader_url


class InfoQuestClient:
    """调用 InfoQuest 搜索与网页抓取 API。"""

    def __init__(self, fetch_time: int = -1, fetch_timeout: int = -1, fetch_navigation_timeout: int = -1, search_time_range: int = -1, image_search_time_range: int = -1, image_size: str = "i"):
        logger.info("初始化 BytePlus InfoQuest 客户端")

        self.fetch_time = fetch_time
        self.fetch_timeout = fetch_timeout
        self.fetch_navigation_timeout = fetch_navigation_timeout
        self.search_time_range = search_time_range
        self.image_search_time_range = image_search_time_range
        self.image_size = image_size
        self.api_key_set = bool(os.getenv("INFOQUEST_API_KEY"))
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "InfoQuest 配置 fetch_time=%s fetch_timeout=%s navigation_timeout=%s "
                "search_time_range=%s image_search_time_range=%s image_size=%s api_key_set=%s",
                fetch_time,
                fetch_timeout,
                fetch_navigation_timeout,
                search_time_range,
                image_search_time_range,
                image_size,
                self.api_key_set,
            )

    def fetch(self, url: str, return_format: str = "html") -> str:
        if logger.isEnabledFor(logging.DEBUG):
            url_truncated = url[:50] + "..." if len(url) > 50 else url
            logger.debug(
                f"InfoQuest - Fetch API request initiated | "
                f"operation=crawl url | "
                f"url_truncated={url_truncated} | "
                f"has_timeout_filter={self.fetch_timeout > 0} | timeout_filter={self.fetch_timeout} | "
                f"has_fetch_time_filter={self.fetch_time > 0} | fetch_time_filter={self.fetch_time} | "
                f"has_navigation_timeout_filter={self.fetch_navigation_timeout > 0} | navi_timeout_filter={self.fetch_navigation_timeout} | "
                f"request_type=sync"
            )

        # 准备请求头。
        headers = self._prepare_headers()

        # 准备请求体。
        data = self._prepare_crawl_request_data(url, return_format)

        logger.debug("Sending crawl request to InfoQuest API")
        try:
            response = requests.post(INFOQUEST_READER_URL, headers=headers, json=data)

            # 非 200 响应按上游调用失败处理。
            if response.status_code != 200:
                error_message = f"fetch API returned status {response.status_code}: {response.text}"
                logger.debug("InfoQuest Crawler fetch API return status %d: %s for URL: %s", response.status_code, response.text, url)
                return f"Error: {error_message}"

            # 空响应不能作为有效检索结果。
            if not response.text or not response.text.strip():
                error_message = "no result found"
                logger.debug("InfoQuest Crawler returned empty response for URL: %s", url)
                return f"Error: {error_message}"

            # 优先从 JSON 响应提取 reader_result。
            try:
                response_data = json.loads(response.text)
                # 优先返回 reader_result。
                if "reader_result" in response_data:
                    logger.debug("Successfully extracted reader_result from JSON response")
                    return response_data["reader_result"]
                elif "content" in response_data:
                    # reader_result 缺失时降级使用 content。
                    logger.debug("reader_result missing in JSON response, falling back to content field: %s", response_data["content"])
                    return response_data["content"]
                else:
                    # 两个字段均缺失时保留原始响应。
                    logger.warning("Neither reader_result nor content field found in JSON response")
            except json.JSONDecodeError:
                # 非 JSON 响应按原始文本返回。
                logger.debug("Response is not in JSON format, returning as-is")
                return response.text

            # 日志仅记录截断后的响应，避免输出过多上游内容。
            if logger.isEnabledFor(logging.DEBUG):
                response_sample = response.text[:200] + ("..." if len(response.text) > 200 else "")
                logger.debug("Successfully received response, content length: %d bytes, first 200 chars: %s", len(response.text), response_sample)
            return response.text
        except Exception as e:
            error_message = f"fetch API failed: {str(e)}"
            logger.error(error_message)
            return f"Error: {error_message}"

    @staticmethod
    def _prepare_headers() -> dict[str, str]:
        """构造 InfoQuest 请求头。"""
        headers = {
            "Content-Type": "application/json",
        }

        # 仅在已配置时添加 API Key。
        if os.getenv("INFOQUEST_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('INFOQUEST_API_KEY')}"
            logger.debug("API key added to request headers")
        else:
            logger.warning("InfoQuest API key is not set. Provide your own key for authentication.")

        return headers

    def _prepare_crawl_request_data(self, url: str, return_format: str) -> dict[str, Any]:
        """构造并规范化网页抓取请求参数。"""
        # 规范化返回格式。
        if return_format and return_format.lower() == "html":
            normalized_format = "HTML"
        else:
            normalized_format = return_format

        data = {"url": url, "format": normalized_format}

        # 仅透传正数超时参数。
        timeout_params = {}
        if self.fetch_time > 0:
            timeout_params["fetch_time"] = self.fetch_time
        if self.fetch_timeout > 0:
            timeout_params["timeout"] = self.fetch_timeout
        if self.fetch_navigation_timeout > 0:
            timeout_params["navi_timeout"] = self.fetch_navigation_timeout

        # 记录实际生效的超时参数。
        if timeout_params:
            logger.debug("Applying timeout parameters: %s", timeout_params)
            data.update(timeout_params)

        return data

    def web_search_raw_results(
        self,
        query: str,
        site: str,
        output_format: str = "JSON",
    ) -> dict:
        """同步获取 InfoQuest 网页搜索原始结果。"""
        headers = self._prepare_headers()

        params = {"format": output_format, "query": query}
        if self.search_time_range > 0:
            params["time_range"] = self.search_time_range

        if site != "":
            params["site"] = site

        response = requests.post(INFOQUEST_SEARCH_URL, headers=headers, json=params)
        response.raise_for_status()

        # 日志仅记录截断后的响应。
        response_json = response.json()
        if logger.isEnabledFor(logging.DEBUG):
            response_sample = json.dumps(response_json)[:200] + ("..." if len(json.dumps(response_json)) > 200 else "")
            logger.debug(f"Search API request completed successfully | service=InfoQuest | status=success | response_sample={response_sample}")

        return response_json

    @staticmethod
    def clean_results(raw_results: list[dict[str, dict[str, dict[str, Any]]]]) -> list[dict]:
        """清洗 InfoQuest 网页搜索结果。"""
        logger.debug("Processing web-search results")

        seen_urls = set()
        clean_results = []
        counts = {"pages": 0, "news": 0}

        for content_list in raw_results:
            content = content_list["content"]
            results = content["results"]

            if results.get("organic"):
                organic_results = results["organic"]
                for result in organic_results:
                    clean_result = {
                        "type": "page",
                    }
                    if "title" in result:
                        clean_result["title"] = result["title"]
                    if "desc" in result:
                        clean_result["desc"] = result["desc"]
                        clean_result["snippet"] = result["desc"]
                    if "url" in result:
                        clean_result["url"] = result["url"]
                        url = clean_result["url"]
                        if isinstance(url, str) and url and url not in seen_urls:
                            seen_urls.add(url)
                            clean_results.append(clean_result)
                            counts["pages"] += 1

            if results.get("top_stories"):
                news = results["top_stories"]
                for obj in news["items"]:
                    clean_result = {
                        "type": "news",
                    }
                    if "time_frame" in obj:
                        clean_result["time_frame"] = obj["time_frame"]
                    if "source" in obj:
                        clean_result["source"] = obj["source"]
                    title = obj.get("title")
                    url = obj.get("url")
                    if title:
                        clean_result["title"] = title
                    if url:
                        clean_result["url"] = url
                    if title and isinstance(url, str) and url and url not in seen_urls:
                        seen_urls.add(url)
                        clean_results.append(clean_result)
                        counts["news"] += 1
        logger.debug(f"Results processing completed | total_results={len(clean_results)} | pages={counts['pages']} | news_items={counts['news']} | unique_urls={len(seen_urls)}")

        return clean_results

    def web_search(
        self,
        query: str,
        site: str = "",
        output_format: str = "JSON",
    ) -> str:
        if logger.isEnabledFor(logging.DEBUG):
            query_truncated = query[:50] + "..." if len(query) > 50 else query
            logger.debug(
                f"InfoQuest - Search API request initiated | "
                f"operation=search webs | "
                f"query_truncated={query_truncated} | "
                f"has_time_filter={self.search_time_range > 0} | time_filter={self.search_time_range} | "
                f"has_site_filter={bool(site)} | site={site} | "
                f"request_type=sync"
            )

        try:
            logger.debug("InfoQuest Web-Search - Executing search with parameters")
            raw_results = self.web_search_raw_results(
                query,
                site,
                output_format,
            )
            if "search_result" in raw_results:
                logger.debug("InfoQuest Web-Search - Successfully extracted search_result from JSON response")
                results = raw_results["search_result"]

                logger.debug("InfoQuest Web-Search - Processing raw search results")
                cleaned_results = self.clean_results(results["results"])

                result_json = json.dumps(cleaned_results, indent=2, ensure_ascii=False)

                logger.debug(f"InfoQuest Web-Search - Search tool execution completed | mode=synchronous | results_count={len(cleaned_results)}")
                return result_json

            elif "content" in raw_results:
                # search_result 缺失时降级使用 content。
                error_message = "web search API return wrong format"
                logger.error("web search API return wrong format, no search_result nor content field found in JSON response, content: %s", raw_results["content"])
                return f"Error: {error_message}"
            else:
                # 两个字段均缺失时保留原始响应。
                logger.warning("InfoQuest Web-Search - Neither search_result nor content field found in JSON response")
                return json.dumps(raw_results, indent=2, ensure_ascii=False)

        except Exception as e:
            error_message = f"InfoQuest Web-Search - Search tool execution failed | mode=synchronous | error={str(e)}"
            logger.error(error_message)
            return f"Error: {error_message}"

    @staticmethod
    def clean_results_with_image_search(raw_results: list[dict[str, dict[str, dict[str, Any]]]]) -> list[dict]:
        """清洗 InfoQuest 图片搜索结果。"""
        logger.debug("Processing web-search results")

        seen_urls = set()
        clean_results = []
        counts = {"images": 0}

        for content_list in raw_results:
            content = content_list["content"]
            results = content["results"]

            if results.get("images_results"):
                images_results = results["images_results"]
                for result in images_results:
                    clean_result = {}
                    if "original" in result:
                        clean_result["image_url"] = result["original"]
                        url = clean_result["image_url"]
                        if isinstance(url, str) and url and url not in seen_urls:
                            seen_urls.add(url)
                            clean_results.append(clean_result)
                            counts["images"] += 1
                    if "title" in result:
                        clean_result["title"] = result["title"]
        logger.debug(f"Results processing completed | total_results={len(clean_results)} | images={counts['images']} | unique_urls={len(seen_urls)}")

        return clean_results

    def image_search_raw_results(
        self,
        query: str,
        site: str = "",
        output_format: str = "JSON",
    ) -> dict:
        """同步获取 InfoQuest 图片搜索原始结果。"""
        headers = self._prepare_headers()

        params = {"format": output_format, "query": query, "search_type": "Images"}

        # 仅接受 1 至 365 天的时间范围过滤。
        if 1 <= self.image_search_time_range <= 365:
            params["time_range"] = self.image_search_time_range
        elif self.image_search_time_range > 0:
            logger.warning(f"time_range {self.image_search_time_range} is out of valid range (1-365), ignoring")

        # 按需添加站点过滤。
        if site:
            params["site"] = site

        # 按需添加图片尺寸过滤。
        if self.image_size and self.image_size in ["l", "m", "i"]:
            params["image_size"] = self.image_size
        elif self.image_size:
            logger.warning(f"image_size {self.image_size} is not valid, must be 'l', 'm', or 'i'")

        response = requests.post(INFOQUEST_SEARCH_URL, headers=headers, json=params)
        response.raise_for_status()

        # 日志仅记录截断后的响应。
        response_json = response.json()
        if logger.isEnabledFor(logging.DEBUG):
            response_sample = json.dumps(response_json)[:200] + ("..." if len(json.dumps(response_json)) > 200 else "")
            logger.debug(f"Image Search API request completed successfully | service=InfoQuest | status=success | response_sample={response_sample}")

        return response_json

    def image_search(
        self,
        query: str,
        site: str = "",
        output_format: str = "JSON",
    ) -> str:
        if logger.isEnabledFor(logging.DEBUG):
            query_truncated = query[:50] + "..." if len(query) > 50 else query
            logger.debug(
                f"InfoQuest - Image Search API request initiated | "
                f"operation=search images | "
                f"query_truncated={query_truncated} | "
                f"has_site_filter={bool(site)} | site={site} | "
                f"image_search_time_range={self.image_search_time_range if self.image_search_time_range >= 1 and self.image_search_time_range <= 365 else 'default'} | "
                f"image_size={self.image_size} |"
                f"request_type=sync"
            )

        try:
            logger.info("InfoQuest Image Search - Executing search with parameters")
            raw_results = self.image_search_raw_results(
                query,
                site,
                output_format,
            )

            if "search_result" in raw_results:
                logger.debug("InfoQuest Image Search - Successfully extracted search_result from JSON response")
                results = raw_results["search_result"]

                logger.debug(f"InfoQuest Image Search - Processing raw image search results: {results}")
                cleaned_results = self.clean_results_with_image_search(results["results"])

                result_json = json.dumps(cleaned_results, indent=2, ensure_ascii=False)

                logger.debug(f"InfoQuest Image Search - Image search tool execution completed | mode=synchronous | results_count={len(cleaned_results)}")
                return result_json

            elif "content" in raw_results:
                # search_result 缺失时降级使用 content。
                error_message = "image search API return wrong format"
                logger.error("image search API return wrong format, no search_result nor content field found in JSON response, content: %s", raw_results["content"])
                return f"Error: {error_message}"
            else:
                # 两个字段均缺失时保留原始响应。
                logger.warning("InfoQuest Image Search - Neither search_result nor content field found in JSON response")
                return json.dumps(raw_results, indent=2, ensure_ascii=False)

        except Exception as e:
            error_message = f"InfoQuest Image Search - Image search tool execution failed | mode=synchronous | error={str(e)}"
            logger.error(error_message)
            return f"Error: {error_message}"
