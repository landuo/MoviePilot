from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

from app.core.context import TorrentInfo
from app.db.site_oper import SiteOper
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper  # noqa
from app.log import logger
from app.modules import _ModuleBase
from app.modules.indexer.parser import SiteParserBase
from app.modules.indexer.spider import SiteSpider
from app.modules.indexer.spider.haidan import HaiDanSpider
from app.modules.indexer.spider.hddolby import HddolbySpider
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.modules.indexer.spider.rousi import RousiSpider
from app.modules.indexer.spider.tnode import TNodeSpider
from app.modules.indexer.spider.torrentleech import TorrentLeech
from app.modules.indexer.spider.yema import YemaSpider
from app.schemas import SiteUserData
from app.schemas.types import MediaType, ModuleType, OtherModulesType
from app.utils.string import StringUtils


SPIDER_PARSER_CLASSES = {
    "TNodeSpider": TNodeSpider,
    "TorrentLeech": TorrentLeech,
    "mTorrent": MTorrentSpider,
    "Yema": YemaSpider,
    "Haidan": HaiDanSpider,
    "HDDolby": HddolbySpider,
    "RousiPro": RousiSpider,
}


class IndexerModule(_ModuleBase):
    """
    索引模块
    """

    _site_schemas = []

    def init_module(self) -> None:
        # 加载模块
        self._site_schemas = ModuleHelper.load(
            'app.modules.indexer.parser',
            filter_func=lambda _, obj: hasattr(obj, 'schema') and getattr(obj, 'schema') is not None)
        pass

    @staticmethod
    def get_name() -> str:
        return "站点索引"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型
        """
        return ModuleType.Indexer

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型
        """
        return OtherModulesType.Indexer

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 0

    def stop(self):
        pass

    def test(self) -> Tuple[bool, str]:
        """
        测试模块连接性
        """
        sites = SitesHelper().get_indexers()
        if not sites:
            return False, "未配置站点或未通过用户认证"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def __search_check(site: dict, search_word: Optional[str] = None) -> bool:
        """
        检查是否可以执行搜索
        """
        # 可能为关键字或ttxxxx
        if search_word \
                and site.get('language') == "en" \
                and StringUtils.is_chinese(search_word):
            # 不支持中文
            logger.warn(f"{site.get('name')} 不支持中文搜索")
            return False

        # 站点流控
        state, msg = SitesHelper().check(StringUtils.get_url_domain(site.get("domain")))
        if state:
            logger.warn(msg)
            return False

        return True

    @staticmethod
    def __clear_search_text(text: Optional[str]) -> Optional[str]:
        """
        清理搜索文本
        :param text: 需要清理的文本
        :return: 清理后的文本
        """
        if not text:
            return text
        # 去除特殊字符和多余空格
        return StringUtils.clear(text, replace_word=" ", allow_space=True)

    @staticmethod
    def __indexer_statistic(site: dict, error_flag: bool = False, seconds: int = 0) -> None:
        """
        索引器统计
        """
        domain = StringUtils.get_url_domain(site.get("domain"))
        if error_flag:
            SiteOper().fail(domain)
        else:
            SiteOper().success(domain=domain, seconds=seconds)

    @staticmethod
    async def __async_indexer_statistic(site: dict, error_flag: bool = False, seconds: int = 0) -> None:
        """
        异步索引器统计
        """
        domain = StringUtils.get_url_domain(site.get("domain"))
        if error_flag:
            await SiteOper().async_fail(domain)
        else:
            await SiteOper().async_success(domain=domain, seconds=seconds)

    @staticmethod
    def __parse_result(site: dict, result_array: list, seconds: int) -> TorrentInfo:
        """
        解析搜索结果为 TorrentInfo 对象
        """
        if not result_array or len(result_array) == 0:
            logger.warn(f"{site.get('name')} 未搜索到数据，耗时 {seconds} 秒")
            return []
        logger.info(
            f"{site.get('name')} 搜索完成，耗时 {seconds} 秒，返回数据：{len(result_array)}")
        return [TorrentInfo(site=site.get("id"),
                            site_name=site.get("name"),
                            site_cookie=site.get("cookie"),
                            site_ua=site.get("ua"),
                            site_proxy=site.get("proxy"),
                            site_order=site.get("pri"),
                            site_downloader=site.get("downloader"),
                            **result) for result in result_array]

    @staticmethod
    def get_search_page_size(site: dict, keyword: Optional[str] = None) -> Optional[int]:
        """
        获取站点搜索单页容量；None 表示当前搜索入口不支持可靠翻页。
        """
        site = site or {}
        parser = site.get("parser")
        if parser in SPIDER_PARSER_CLASSES:
            return SPIDER_PARSER_CLASSES[parser].get_search_page_size(keyword=keyword)
        try:
            page_size = int(site.get("result_num") or SiteSpider.default_result_num())
        except (TypeError, ValueError):
            page_size = SiteSpider.default_result_num()
        return page_size if page_size > 0 else SiteSpider.default_result_num()

    def search_torrents(self, site: dict,
                        keyword: str = None,
                        mtype: MediaType = None,
                        cat: Optional[str] = None,
                        page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        搜索一个站点
        :param site:  站点
        :param keyword:  搜索关键词
        :param mtype:  媒体类型
        :param cat:  分类
        :param page:  页码
        :return: 资源列表
        """

        # 索引结果
        result = []
        # 开始计时
        start_time = datetime.now()
        # 错误标志
        error_flag = False

        # 检查是否可以执行搜索
        if not self.__search_check(site, keyword):
            return []

        # 去除搜索关键字中的特殊字符
        search_word = self.__clear_search_text(keyword)

        # 开始搜索
        try:
            if site.get('parser') == "TNodeSpider":
                error_flag, result = TNodeSpider(site).search(
                    keyword=search_word,
                    page=page
                )
            elif site.get('parser') == "TorrentLeech":
                error_flag, result = TorrentLeech(site).search(
                    keyword=search_word,
                    page=page
                )
            elif site.get('parser') == "mTorrent":
                error_flag, result = MTorrentSpider(site).search(
                    keyword=search_word,
                    mtype=mtype,
                    page=page
                )
            elif site.get('parser') == "Yema":
                error_flag, result = YemaSpider(site).search(
                    keyword=search_word,
                    mtype=mtype,
                    page=page
                )
            elif site.get('parser') == "Haidan":
                error_flag, result = HaiDanSpider(site).search(
                    keyword=search_word,
                    mtype=mtype
                )
            elif site.get('parser') == "HDDolby":
                error_flag, result = HddolbySpider(site).search(
                    keyword=search_word,
                    mtype=mtype,
                    page=page
                )
            elif site.get('parser') == "RousiPro":
                error_flag, result = RousiSpider(site).search(
                    keyword=search_word,
                    mtype=mtype,
                    cat=cat,
                    page=page
                )
            else:
                error_flag, result = self.__spider_search(
                    search_word=search_word,
                    indexer=site,
                    mtype=mtype,
                    cat=cat,
                    page=page
                )
        except Exception as err:
            logger.error(f"{site.get('name')} 搜索出错：{str(err)}")

        # 索引花费的时间
        seconds = (datetime.now() - start_time).seconds

        # 统计索引情况
        self.__indexer_statistic(site=site, error_flag=error_flag, seconds=seconds)

        # 返回结果
        return self.__parse_result(
            site=site,
            result_array=result,
            seconds=seconds
        )

    async def async_search_torrents(self, site: dict,
                                    keyword: str = None,
                                    mtype: MediaType = None,
                                    cat: Optional[str] = None,
                                    page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        异步搜索一个站点
        :param site:  站点
        :param keyword:  搜索关键词
        :param mtype:  媒体类型
        :param cat:  分类
        :param page:  页码
        :return: 资源列表
        """

        # 索引结果
        result = []
        # 开始计时
        start_time = datetime.now()
        # 错误标志
        error_flag = False

        # 检查是否可以执行搜索
        if not self.__search_check(site, keyword):
            return []

        # 去除搜索关键字中的特殊字符
        search_word = self.__clear_search_text(keyword)

        # 开始搜索
        try:
            if site.get('parser') == "TNodeSpider":
                error_flag, result = await TNodeSpider(site).async_search(
                    keyword=search_word,
                    page=page
                )
            elif site.get('parser') == "TorrentLeech":
                error_flag, result = await TorrentLeech(site).async_search(
                    keyword=search_word,
                    page=page
                )
            elif site.get('parser') == "mTorrent":
                error_flag, result = await MTorrentSpider(site).async_search(
                    keyword=search_word,
                    mtype=mtype,
                    page=page
                )
            elif site.get('parser') == "Yema":
                error_flag, result = await YemaSpider(site).async_search(
                    keyword=search_word,
                    mtype=mtype,
                    page=page
                )
            elif site.get('parser') == "Haidan":
                error_flag, result = await HaiDanSpider(site).async_search(
                    keyword=search_word,
                    mtype=mtype
                )
            elif site.get('parser') == "HDDolby":
                error_flag, result = await HddolbySpider(site).async_search(
                    keyword=search_word,
                    mtype=mtype,
                    page=page
                )
            elif site.get('parser') == "RousiPro":
                error_flag, result = await RousiSpider(site).async_search(
                    keyword=search_word,
                    mtype=mtype,
                    cat=cat,
                    page=page
                )
            else:
                error_flag, result = await self.__async_spider_search(
                    search_word=search_word,
                    indexer=site,
                    mtype=mtype,
                    cat=cat,
                    page=page
                )
        except Exception as err:
            logger.error(f"{site.get('name')} 搜索出错：{str(err)}")

        # 索引花费的时间
        seconds = (datetime.now() - start_time).seconds

        # 统计索引情况
        await self.__async_indexer_statistic(site=site, error_flag=error_flag, seconds=seconds)

        # 返回结果
        return self.__parse_result(
            site=site,
            result_array=result,
            seconds=seconds
        )

    # 使用通用 SiteSpider（即非特殊 parser）的站点，可以走批量 worker 加速
    _SPECIAL_PARSERS = frozenset({
        "TNodeSpider", "TorrentLeech", "mTorrent",
        "Yema", "Haidan", "HDDolby", "RousiPro",
    })

    def batch_search_torrents(self, sites: List[dict],
                              keyword: str = None,
                              mtype: MediaType = None,
                              cat: Optional[str] = None,
                              page: Optional[int] = 0) -> Dict[int, List[TorrentInfo]]:
        """
        批量搜索多个站点。

        优化路径（mp-indexer 可用时）：
          1. 把使用通用 SiteSpider 的站点拼一次 worker 调用，Go 端 goroutine 并发拿 HTML
          2. Python 端按站点并行解析 HTML（CPU 密集，多线程仍有收益）
          3. 特殊 Spider 站点和 worker 失败的站点 fallback 到 search_torrents 单站点路径

        Fallback 路径（worker 不可用时）：
          全部站点走 search_torrents 原路径（调用方负责并发调度）

        :param sites:  站点配置列表
        :param keyword:  搜索关键词
        :param mtype:  媒体类型
        :param cat:  分类
        :param page:  页码
        :return: {site_id: [TorrentInfo, ...]} 站点 ID → 资源列表
        """
        results: Dict[int, List[TorrentInfo]] = {}
        if not sites:
            return results

        # 预筛：通用 SiteSpider 的站点 vs 特殊 Spider 的站点
        general_sites = [s for s in sites if s.get("parser") not in self._SPECIAL_PARSERS]
        special_sites = [s for s in sites if s.get("parser") in self._SPECIAL_PARSERS]

        # 检查 worker 是否可用，不可用则全部走老路径
        use_worker = self.__is_indexer_worker_available()
        # 可观测计数：worker 命中 / 单站点 fallback / 特殊 spider
        worker_hit = 0
        worker_miss = 0
        start_ts = time.time()

        if use_worker and general_sites:
            # 批量获取通用站点的 HTML
            html_map = self.__batch_fetch_html(
                sites=general_sites, keyword=keyword, mtype=mtype, cat=cat, page=page,
            )
            # 解析 HTML / 单站点 fallback
            for site in general_sites:
                site_id = site.get("id")
                html = html_map.get(site_id) if html_map else None
                if html:
                    # 走解析路径
                    worker_hit += 1
                    results[site_id] = self.__parse_html_to_torrents(
                        site=site, html=html, keyword=keyword, mtype=mtype, cat=cat, page=page,
                    )
                else:
                    # 该站点 worker 失败，单独 fallback
                    worker_miss += 1
                    results[site_id] = self.search_torrents(
                        site=site, keyword=keyword, mtype=mtype, cat=cat, page=page,
                    ) or []
        else:
            # worker 不可用：通用站点也走老路径
            for site in general_sites:
                results[site.get("id")] = self.search_torrents(
                    site=site, keyword=keyword, mtype=mtype, cat=cat, page=page,
                ) or []

        # 特殊 Spider 始终走老路径
        for site in special_sites:
            results[site.get("id")] = self.search_torrents(
                site=site, keyword=keyword, mtype=mtype, cat=cat, page=page,
            ) or []

        # 汇总日志：让运维 / 自测时直接看到"批量优化是否生效"
        elapsed_ms = int((time.time() - start_ts) * 1000)
        if use_worker:
            logger.info(
                f"[indexer-batch] 共 {len(sites)} 站点 | "
                f"worker 命中 {worker_hit} | worker 失败回退 {worker_miss} | "
                f"特殊 spider 直走 {len(special_sites)} | 耗时 {elapsed_ms}ms"
            )
        else:
            logger.info(
                f"[indexer-batch] 共 {len(sites)} 站点 | worker 不可用，全部走旧路径 | "
                f"通用 {len(general_sites)} | 特殊 {len(special_sites)} | 耗时 {elapsed_ms}ms"
            )

        return results

    async def async_batch_search_torrents(self, sites: List[dict],
                                          keyword: str = None,
                                          mtype: MediaType = None,
                                          cat: Optional[str] = None,
                                          page: Optional[int] = 0) -> Dict[int, List[TorrentInfo]]:
        """
        异步批量搜索多个站点。

        语义与 batch_search_torrents 完全一致，仅在 fallback 时使用 async_search_torrents。
        worker 调用本身是阻塞的（HTTP over UDS），通过 run_in_threadpool 卸到线程池避免阻塞 event loop。

        :return: {site_id: [TorrentInfo, ...]}
        """
        results: Dict[int, List[TorrentInfo]] = {}
        if not sites:
            return results

        general_sites = [s for s in sites if s.get("parser") not in self._SPECIAL_PARSERS]
        special_sites = [s for s in sites if s.get("parser") in self._SPECIAL_PARSERS]

        use_worker = self.__is_indexer_worker_available()
        worker_hit = 0
        worker_miss = 0
        start_ts = time.time()

        if use_worker and general_sites:
            # 同步阻塞调用 worker，卸到线程池
            html_map = await run_in_threadpool(
                self.__batch_fetch_html,
                sites=general_sites, keyword=keyword, mtype=mtype, cat=cat, page=page,
            )
            for site in general_sites:
                site_id = site.get("id")
                html = html_map.get(site_id) if html_map else None
                if html:
                    worker_hit += 1
                    # 解析也卸到线程池（PyQuery 是 CPU 密集，会阻塞 event loop）
                    results[site_id] = await run_in_threadpool(
                        self.__parse_html_to_torrents,
                        site=site, html=html, keyword=keyword, mtype=mtype, cat=cat, page=page,
                    )
                else:
                    worker_miss += 1
                    results[site_id] = await self.async_search_torrents(
                        site=site, keyword=keyword, mtype=mtype, cat=cat, page=page,
                    ) or []
        else:
            for site in general_sites:
                results[site.get("id")] = await self.async_search_torrents(
                    site=site, keyword=keyword, mtype=mtype, cat=cat, page=page,
                ) or []

        for site in special_sites:
            results[site.get("id")] = await self.async_search_torrents(
                site=site, keyword=keyword, mtype=mtype, cat=cat, page=page,
            ) or []

        elapsed_ms = int((time.time() - start_ts) * 1000)
        if use_worker:
            logger.info(
                f"[indexer-batch-async] 共 {len(sites)} 站点 | "
                f"worker 命中 {worker_hit} | worker 失败回退 {worker_miss} | "
                f"特殊 spider 直走 {len(special_sites)} | 耗时 {elapsed_ms}ms"
            )
        else:
            logger.info(
                f"[indexer-batch-async] 共 {len(sites)} 站点 | worker 不可用，全部走旧路径 | "
                f"通用 {len(general_sites)} | 特殊 {len(special_sites)} | 耗时 {elapsed_ms}ms"
            )

        return results

    async def async_worker_search_site(self, site: dict,
                                       keyword: Optional[str] = None,
                                       mtype: MediaType = None,
                                       cat: Optional[str] = None,
                                       page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        单站点真异步搜索入口（流式批量化使用）

        与 async_search_torrents 的区别：
        - async_search_torrents：内部 worker 调用是同步阻塞的（HTTP over UDS），
          多站点并发时本质上是 ThreadPoolExecutor 串行调度
        - async_worker_search_site：worker 调用与 HTML 解析都通过 run_in_threadpool
          卸到线程池，但作为独立 coroutine 暴露，配合 asyncio.create_task +
          asyncio.as_completed 即可获得真异步并发 + 流式语义

        策略：
        - 特殊 spider（TNode/mTorrent 等）→ 直接走原 async_search_torrents
        - worker 不可用 → 直接走原 async_search_torrents
        - worker 可用且通用 SiteSpider → fetch + parse 卸到线程池
        - 任意失败 → fallback 到 async_search_torrents

        :return: 资源列表（始终返回列表，不抛异常）
        """
        if not site:
            return []

        # 特殊 spider 走原异步路径
        if site.get("parser") in self._SPECIAL_PARSERS:
            return await self.async_search_torrents(
                site=site, keyword=keyword, mtype=mtype, cat=cat, page=page,
            ) or []

        # worker 不可用走原异步路径
        if not self.__is_indexer_worker_available():
            return await self.async_search_torrents(
                site=site, keyword=keyword, mtype=mtype, cat=cat, page=page,
            ) or []

        # worker 路径：单站点 fetch + parse 卸到线程池
        try:
            html_map = await run_in_threadpool(
                self.__batch_fetch_html,
                sites=[site], keyword=keyword, mtype=mtype, cat=cat, page=page,
            )
            html = html_map.get(site.get("id")) if html_map else None
            if html:
                return await run_in_threadpool(
                    self.__parse_html_to_torrents,
                    site=site, html=html, keyword=keyword, mtype=mtype, cat=cat, page=page,
                ) or []
        except Exception as err:
            logger.warn(f"{site.get('name')} worker 异步搜索异常，回退原路径：{err}")

        # worker 失败 fallback
        return await self.async_search_torrents(
            site=site, keyword=keyword, mtype=mtype, cat=cat, page=page,
        ) or []

    @staticmethod
    def __is_indexer_worker_available() -> bool:
        """快速判断 mp-indexer worker 是否可用（不发请求）"""
        try:
            from app.core.config import settings
            from app.utils.worker_client import WorkerClientManager
        except Exception:
            return False
        if not settings.is_worker_enabled("indexer"):
            return False
        client = WorkerClientManager().get("indexer")
        return client.is_available()

    def __batch_fetch_html(self, sites: List[dict],
                           keyword: str = None,
                           mtype: MediaType = None,
                           cat: Optional[str] = None,
                           page: Optional[int] = 0) -> Dict[int, str]:
        """
        通过 mp-indexer worker 批量获取多个站点的 HTML 原文。

        :return: {site_id: html_str}，缺失的 site_id 表示该站点 worker 失败需 fallback
        """
        from app.utils.worker_client import WorkerClientManager

        # 为每个站点构造 SiteSpider，提取 URL + headers + proxy
        # 同时需要做 search_check / clear_search_text 等前置检查（与 search_torrents 对齐）
        search_word = self.__clear_search_text(keyword)

        request_items = []
        spiders_by_id: Dict[int, SiteSpider] = {}

        for site in sites:
            site_id = site.get("id")
            if not self.__search_check(site, keyword):
                continue

            spider = SiteSpider(
                indexer=site, keyword=search_word, mtype=mtype, cat=cat, page=page,
            )
            if not spider.search or not spider.domain:
                continue

            try:
                search_url = spider._SiteSpider__get_search_url()
            except Exception as err:
                logger.warn(f"{site.get('name')} 构造搜索 URL 失败：{str(err)}")
                continue
            if not search_url:
                continue

            headers = {}
            if spider.ua:
                headers["User-Agent"] = spider.ua
            if spider.cookie:
                headers["Cookie"] = spider.cookie
            if spider.referer:
                headers["Referer"] = spider.referer

            proxy = ""
            if spider.proxies:
                proxy = spider.proxies.get("https") or spider.proxies.get("http") or ""

            request_items.append({
                "id": str(site_id),
                "url": search_url,
                "method": "GET",
                "headers": headers,
                "proxy": proxy,
                "timeout_ms": (spider._timeout or 15) * 1000,
                "allow_redirects": True,
            })
            spiders_by_id[site_id] = spider

        if not request_items:
            return {}

        client = WorkerClientManager().get("indexer")
        logger.info(f"通过 mp-indexer 批量请求 {len(request_items)} 个站点")

        try:
            resp_data = client.call_or_raise(
                "/api/v1/fetch", {"requests": request_items},
            )
        except Exception as err:
            logger.warn(f"mp-indexer 批量请求失败：{str(err)}，全部 fallback")
            return {}

        # 解析响应：成功的填入 html_map，失败的不填（让调用方 fallback）
        html_map: Dict[int, str] = {}
        for item in (resp_data or {}).get("results", []) or []:
            try:
                site_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            spider = spiders_by_id.get(site_id)
            if not spider:
                continue
            err = item.get("error", "")
            if err:
                logger.warn(f"mp-indexer 站点 {site_id} 请求失败：{err}")
                spider.is_error = True
                continue
            status_code = item.get("status_code", 0)
            if status_code != 200:
                logger.warn(f"mp-indexer 站点 {site_id} 状态码 {status_code}")
                spider.is_error = True
                continue
            body = item.get("body", "")
            if not body:
                continue
            html_map[site_id] = body
        return html_map

    def __parse_html_to_torrents(self, site: dict, html: str,
                                 keyword: str = None,
                                 mtype: MediaType = None,
                                 cat: Optional[str] = None,
                                 page: Optional[int] = 0) -> List[TorrentInfo]:
        """
        用预取的 HTML 解析单站点结果，复用 SiteSpider.parse 逻辑。
        与 search_torrents 的统计/日志/封装行为对齐。
        """
        start_time = datetime.now()
        error_flag = False
        result_array = []

        search_word = self.__clear_search_text(keyword)
        spider = SiteSpider(
            indexer=site, keyword=search_word, mtype=mtype, cat=cat, page=page,
        )
        try:
            try:
                result_array = spider.parse(html)
                error_flag = spider.is_error
            except Exception as err:
                logger.error(f"{site.get('name')} 解析出错：{str(err)}")
                error_flag = True
        finally:
            del spider

        seconds = (datetime.now() - start_time).seconds
        self.__indexer_statistic(site=site, error_flag=error_flag, seconds=seconds)
        return self.__parse_result(site=site, result_array=result_array, seconds=seconds)

    @staticmethod
    def __spider_search(indexer: dict,
                        search_word: Optional[str] = None,
                        mtype: MediaType = None,
                        cat: Optional[str] = None,
                        page: Optional[int] = 0) -> Tuple[bool, List[dict]]:
        """
        根据关键字搜索单个站点
        :param: indexer: 站点配置
        :param: search_word: 关键字
        :param: cat: 分类
        :param: page: 页码
        :param: mtype: 媒体类型
        :param: timeout: 超时时间
        :return: 是否发生错误, 种子列表
        """
        _spider = SiteSpider(indexer=indexer,
                             keyword=search_word,
                             mtype=mtype,
                             cat=cat,
                             page=page)

        try:
            # 尝试通过 mp-indexer worker 加速 HTTP 请求
            html = IndexerModule.__try_worker_fetch(_spider)
            if html is not None:
                return _spider.is_error, _spider.parse(html)
            # Fallback：走原有 Python HTTP 请求
            return _spider.is_error, _spider.get_torrents()
        finally:
            del _spider

    @staticmethod
    async def __async_spider_search(indexer: dict,
                                    search_word: Optional[str] = None,
                                    mtype: MediaType = None,
                                    cat: Optional[str] = None,
                                    page: Optional[int] = 0) -> Tuple[bool, List[dict]]:
        """
        异步根据关键字搜索单个站点
        :param: indexer: 站点配置
        :param: search_word: 关键字
        :param: cat: 分类
        :param: page: 页码
        :param: mtype: 媒体类型
        :param: timeout: 超时时间
        :return: 是否发生错误, 种子列表
        """
        _spider = SiteSpider(indexer=indexer,
                             keyword=search_word,
                             mtype=mtype,
                             cat=cat,
                             page=page)

        try:
            # 尝试通过 mp-indexer worker 加速 HTTP 请求
            html = IndexerModule.__try_worker_fetch(_spider)
            if html is not None:
                return _spider.is_error, _spider.parse(html)
            # Fallback：走原有 Python 异步 HTTP 请求
            result = await _spider.async_get_torrents()
            return _spider.is_error, result
        finally:
            del _spider

    @staticmethod
    def __try_worker_fetch(spider: SiteSpider) -> Optional[str]:
        """
        尝试通过 mp-indexer worker 发送 HTTP 请求获取页面 HTML。

        返回值约定：
        - 返回 str：worker 成功获取到 HTML 原文，调用方直接 parse
        - 返回 None：worker 不可用或调用失败，调用方应走原有 HTTP 路径
        """
        try:
            from app.core.config import settings
            from app.utils.worker_client import WorkerClientManager
        except Exception:
            return None

        if not settings.is_worker_enabled("indexer"):
            return None

        client = WorkerClientManager().get("indexer")
        if not client.is_available():
            return None

        # 从 spider 内部属性构造请求描述
        if not spider.search or not spider.domain:
            return None

        # 访问私有方法获取搜索 URL（name mangling）
        search_url = spider._SiteSpider__get_search_url()
        if not search_url:
            return None

        # 构造请求头
        headers = {}
        if spider.ua:
            headers["User-Agent"] = spider.ua
        if spider.cookie:
            headers["Cookie"] = spider.cookie
        if spider.referer:
            headers["Referer"] = spider.referer

        # 代理
        proxy = ""
        if spider.proxies:
            proxy = spider.proxies.get("https") or spider.proxies.get("http") or ""

        timeout_ms = (spider._timeout or 15) * 1000

        logger.info(f"通过 mp-indexer 请求：{search_url}")

        try:
            from app.schemas.worker import WorkerError
            resp_data = client.call_or_raise(
                "/api/v1/fetch",
                {
                    "requests": [{
                        "id": spider.indexerid or "default",
                        "url": search_url,
                        "method": "GET",
                        "headers": headers,
                        "proxy": proxy,
                        "timeout_ms": timeout_ms,
                        "allow_redirects": True,
                    }]
                },
            )
        except Exception:
            return None

        # 从响应中提取 HTML
        results = resp_data.get("results", []) if resp_data else []
        if not results:
            return None

        item = results[0]
        if item.get("error"):
            logger.warn(f"mp-indexer 请求失败：{item['error']}")
            spider.is_error = True
            return None

        status_code = item.get("status_code", 0)
        if status_code != 200:
            logger.warn(f"mp-indexer 返回状态码 {status_code}")
            spider.is_error = True
            return None

        body = item.get("body", "")
        if not body:
            return None

        return body

    def refresh_torrents(self, site: dict,
                         keyword: Optional[str] = None,
                         cat: Optional[str] = None,
                         page: Optional[int] = 0) -> Optional[List[TorrentInfo]]:
        """
        获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :param keyword:  关键字
        :param cat:  分类
        :param page:  页码
        :reutrn: 种子资源列表
        """
        return self.search_torrents(site=site, keyword=keyword, cat=cat, page=page)

    async def async_refresh_torrents(self, site: dict,
                                     keyword: Optional[str] = None,
                                     cat: Optional[str] = None,
                                     page: Optional[int] = 0) -> Optional[List[TorrentInfo]]:
        """
        异步获取站点最新一页的种子，多个站点需要多线程处理
        :param site:  站点
        :param keyword:  关键字
        :param cat:  分类
        :param page:  页码
        :reutrn: 种子资源列表
        """
        return await self.async_search_torrents(site=site, keyword=keyword, cat=cat, page=page)

    def refresh_userdata(self, site: dict) -> Optional[SiteUserData]:
        """
        刷新站点的用户数据
        :param site:  站点
        :return: 用户数据
        """

        def __get_site_obj() -> Optional[SiteParserBase]:
            """
            获取站点解析器
            """
            for site_schema in self._site_schemas:
                if site_schema.schema and site_schema.schema.value == site.get("schema"):
                    return site_schema(
                        site_name=site.get("name"),
                        url=site.get("url"),
                        site_cookie=site.get("cookie"),
                        apikey=site.get("apikey"),
                        token=site.get("token"),
                        ua=site.get("ua"),
                        proxy=site.get("proxy"))
            return None

        site_obj = __get_site_obj()
        if not site_obj:
            if not site.get("public"):
                logger.warn(f"站点  {site.get('name')} 未找到站点解析器，schema：{site.get('schema')}")
            return None

        # 获取用户数据
        try:
            logger.info(f"站点 {site.get('name')} 开始以 {site.get('schema')} 模型解析数据...")
            site_obj.parse()
            logger.debug(f"站点 {site.get('name')} 数据解析完成")
            return SiteUserData(
                domain=StringUtils.get_url_domain(site.get("url")),
                userid=site_obj.userid,
                username=site_obj.username,
                user_level=site_obj.user_level,
                join_at=site_obj.join_at,
                upload=site_obj.upload,
                download=site_obj.download,
                ratio=site_obj.ratio,
                bonus=site_obj.bonus,
                seeding=site_obj.seeding,
                seeding_size=site_obj.seeding_size,
                seeding_info=site_obj.seeding_info.copy() if site_obj.seeding_info else [],
                leeching=site_obj.leeching,
                leeching_size=site_obj.leeching_size,
                message_unread=site_obj.message_unread,
                message_unread_contents=site_obj.message_unread_contents.copy() if site_obj.message_unread_contents else [],
                updated_day=datetime.now().strftime('%Y-%m-%d'),
                err_msg=site_obj.err_msg
            )
        finally:
            site_obj.clear()
