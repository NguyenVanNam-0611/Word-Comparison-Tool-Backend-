from src.services.page.page_injector import (
    collect_uid_index_pairs,
    inject_pages,
)

from src.services.page.com_pager import (
    get_pages_via_com_index,
)

__all__ = [
    "get_pages_via_com_index",
    "collect_uid_index_pairs",
    "inject_pages",
]