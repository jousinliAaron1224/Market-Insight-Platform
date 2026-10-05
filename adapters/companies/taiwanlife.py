"""台灣人壽。商品清單與條款清單都是 portal-api 的檔案（PDF）；條款清單每列右側的
「契約條款下載」是超連結，以垂直位置對應到左側商品名稱。
注意：網站的頁面 API（portal-api/Page）會被防火牆擋下，因此檔案編號寫在設定中，
若官網換了檔案編號需手動更新 sources.yaml（健康檢查會在解析不到時發警示）。"""
from adapters.companies import clause_index
from adapters.companies.base import CompanyProductsAdapter


class TaiwanLifeProductsAdapter(CompanyProductsAdapter):
    source_id = "company_taiwanlife_products"
    company = "台灣人壽"
    name_prefix = "台灣人壽"
    domain = "www.taiwanlife.com"

    def clause_index_from(self, content: bytes, url: str) -> dict[str, str]:
        return clause_index.from_pdf_hyperlinks(content)
