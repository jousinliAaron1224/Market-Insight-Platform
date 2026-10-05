"""法商法國巴黎人壽（自家基準）。商品清單網址含 uuid 與時間戳，從「公開資訊」頁找最新連結。"""
from adapters.companies.base import CompanyProductsAdapter


class CardifProductsAdapter(CompanyProductsAdapter):
    source_id = "company_cardif_products"
    company = "法商法國巴黎人壽"
    name_prefix = "法商法國巴黎人壽"
    domain = "life.cardif.com.tw"
