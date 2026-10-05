"""國泰人壽。投資型商品有專屬的商品清單 PDF；條款連結在「資訊公開／保險商品」頁。"""
from adapters.companies.base import CompanyProductsAdapter


class CathayProductsAdapter(CompanyProductsAdapter):
    source_id = "company_cathay_products"
    company = "國泰人壽"
    name_prefix = "國泰人壽"
    domain = "www.cathaylife.com.tw"
