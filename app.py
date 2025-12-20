from flask import Flask, render_template, request
import requests
import time
from lxml import etree
from google import genai 
import os
from dotenv import load_dotenv

# ===== Gemini API設定 =====
load_dotenv()
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEYが.envファイルに設定されていません。")
client = genai.Client(api_key=GOOGLE_API_KEY)

app = Flask(__name__)


# ===== アブストラクトを翻訳 =====
def translate_to_japanese(text):
    if not text or text.strip() == "(No abstract)":
        return "(翻訳なし)"
    try:
        response = client.models.generate_content( 
            model="gemini-2.5-flash", # ここでモデルを指定 
            contents=( "次の文章を自然な日本語に翻訳してください。この際、要約はせず、元の文章の全ての情報を含めてください。\n\n" f"{text}" ) ) 
        return response.text.strip()
    except Exception as e:
        print("翻訳エラー:", e)
        return "(翻訳に失敗しました)"

# ===== Geminiで要約 =====
def summarize_text(text):
    if not text or text.strip() == "(No abstract)":
        return "(要約なし)"
    try:
        response = client.models.generate_content( 
            model="gemini-2.5-flash", # ここでモデルを指定 
            contents=( "次のPubMed論文のアブストラクトを日本語で100文字以内に要約してください。\n\n" f"{text}" ) ) 
        return response.text.strip()
    except Exception as e:
        print("Gemini要約エラー:", e)
        return "(要約生成に失敗しました)"

# ===== PMCから図（Figure）URLを取得 =====
def get_figures_from_pmc(pmid):
    try:
        print(f"--- {pmid} の図をチェック中 ---") # ステップ1: 開始の確認

        # 1️⃣ PMID → PMCIDの対応を取得
        idconv_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/?ids={pmid}&format=json"
        res = requests.get(idconv_url)
        res_json = res.json()
        records = res_json.get("records", [])

        print(f"ステップ1: ID変換URL: {idconv_url}")
        print(f"ステップ1: ID変換結果: {res_json}")

        if not records or "pmcid" not in records[0]:
            print(f"ステップ1失敗: PMID {pmid} はPMCIDが見つかりませんでした。")
            return []  # PMC未登録

        pmcid = records[0]["pmcid"].replace("PMC", "")
        print(f"ステップ1成功: PMCID: {pmcid}")

        # 2️⃣ PMC XML取得
        fetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&retmode=xml&id={pmcid}"
        xml_data = requests.get(fetch_url).content
        print(f"ステップ2: XML取得URL: {fetch_url}")

        # XMLデータが空でないかチェック
        if not xml_data:
            print(f"ステップ2失敗: XMLデータが空でした。")
            return []
        
        root = etree.fromstring(xml_data)
        print(f"ステップ2成功: XMLデータを解析しました。")

        # 3️⃣ Figure画像を抽出
        figures = []
        for fig in root.findall(".//fig"):
            label = fig.findtext("label", default="")
            caption = " ".join(fig.xpath(".//caption//text()")) or ""
            graphic = fig.find(".//graphic")
            if graphic is not None:
                href = graphic.get("{http://www.w3.org/1999/xlink}href")
                print(f"ステップ3: 抽出されたLabel: {label}, href: {href}")

                if href:
                    # 画像URLを生成（PMCの画像ホスト）
                    img_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmcid}/bin/{href}"
                    figures.append({"url": img_url, "caption": caption or label})
                    print(f"ステップ3成功: 画像URLを追加: {img_url}")
                else:
                    print(f"ステップ3失敗: graphicタグは見つかったがhref属性がありませんでした。")
            else:
                 print(f"ステップ3失敗: Label {label} の図にgraphicタグが見つかりませんでした。")
        return figures
    except Exception as e:
        print(f"PMC画像抽出エラー({pmid}):", e)
        return []

# ===== PubMed情報取得 + Gemini要約 + 図抽出 =====
def get_paper_info(keywords, retstart=0, retmax=4):
    term = ' OR '.join(f'({kw})' for kw in keywords)
    print(f"🔍 検索クエリ: {term}")

    # PMID検索
    search_url = (
        f'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi'
        f'?db=pubmed&retmode=json&retstart={retstart}&retmax={retmax}&term={term}&sort=relevance'
    )
    response = requests.get(search_url)
    pmids = response.json().get('esearchresult', {}).get('idlist', [])

    if not pmids:
        return []

    # サマリー取得
    summary_url = (
        'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi'
        f'?db=pubmed&retmode=json&id={",".join(pmids)}'
    )
    summary_data = requests.get(summary_url).json().get('result', {})

    # アブストラクト取得
    fetch_url = (
        'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi'
        f'?db=pubmed&retmode=xml&id={",".join(pmids)}'
    )
    root = etree.fromstring(requests.get(fetch_url).content)

    papers = []
    for pmid in pmids:
        title = summary_data.get(pmid, {}).get('title', '(No title)')
        author = summary_data.get(pmid, {}).get('sortfirstauthor', '(Unknown)')
        url = f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/'

        abst_node = root.xpath(f".//PubmedArticle[.//PMID='{pmid}']//Abstract//text()")
        abstract = ' '.join(abst_node) if abst_node else '(No abstract)'

        # 🧠 Gemini要約
        ai_summary = summarize_text(abstract)
        
        # 🌏 英文アブストラクト → 日本語翻訳
        ja_abstract = translate_to_japanese(abstract)

        # 🖼️ 図を取得（PMC）
        #figures = get_figures_from_pmc(pmid)

        papers.append({
            'title': title,
            'authors': author,
            'url': url,
            'summary': ja_abstract,
            #'figures': figures,
            'ai_summary': ai_summary
        })

        time.sleep(1)

    return papers

@app.route('/', methods=['GET', 'POST'])
def index():
    papers = []
    query = ''
    if request.method == 'POST':
        query = request.form.get('keywords', '').strip()
        if query:
            keywords = query.split()
            papers = get_paper_info(keywords)
    return render_template('index.html', papers=papers, query=query)

if __name__ == '__main__':
    app.run(debug=True)
