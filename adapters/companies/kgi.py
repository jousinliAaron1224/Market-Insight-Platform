"""凱基人壽（原中國人壽）。投資型商品有專屬清單 PDF；條款連結在「資訊公開／契約條款」頁。"""
from adapters.companies.base import CompanyProductsAdapter


class KgiProductsAdapter(CompanyProductsAdapter):
    source_id = "company_kgi_products"
    company = "凱基人壽"
    name_prefix = "凱基人壽"
    domain = "www.kgilife.com.tw"
