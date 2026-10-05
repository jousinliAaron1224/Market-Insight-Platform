"""富邦人壽。全險種一份商品清單 PDF，檔名每次更新都會變，從「資訊公開／保險商品」頁找最新連結；
條款連結也在同一頁（連結文字尾巴帶「pdf」，比對時已去除）。"""
from adapters.companies.base import CompanyProductsAdapter


class FubonProductsAdapter(CompanyProductsAdapter):
    source_id = "company_fubon_products"
    company = "富邦人壽"
    name_prefix = "富邦人壽"
    domain = "www.fubon.com"
