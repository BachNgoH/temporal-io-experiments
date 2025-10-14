import os
from typing import Any

from temporalio import activity
from temporal_app.activities.common.browser import launch_chromium_page


def build_search_url(search_term: str, search_type: str) -> str:
    base_url = "https://thuvienphapluat.vn/ma-so-thue"
    path = "/tra-cuu-ma-so-thue-doanh-nghiep"
    mapping = {"company_name": "ten-doanh-nghiep", "tax_code": "ma-so-thue"}
    from urllib.parse import urlencode
    timtheo = mapping.get((search_type or "company_name").lower(), "ten-doanh-nghiep")
    params = {"timtheo": timtheo, "tukhoa": search_term}
    return f"{base_url}{path}?{urlencode(params)}"


async def extract_search_results(page) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    await page.wait_for_timeout(3000)
    table = page.locator('table')
    if not await table.count():
        return results
    rows = table.locator('tbody tr')
    row_count = await rows.count()
    for i in range(row_count):
        row = rows.nth(i)
        cells = row.locator('td')
        if await cells.count() >= 4:
            tax_code_cell = cells.nth(1)
            tax_code_link = tax_code_cell.locator('a')
            if await tax_code_link.count():
                tax_code = (await tax_code_link.text_content()) or ""
                detail_url = await tax_code_link.get_attribute('href')
            else:
                tax_code = (await tax_code_cell.text_content()) or ""
                detail_url = None
            company_name_cell = cells.nth(2)
            company_name_link = company_name_cell.locator('a')
            if await company_name_link.count():
                company_name = (await company_name_link.text_content()) or ""
            else:
                company_name = (await company_name_cell.text_content()) or ""
            issue_date = (await cells.nth(3).text_content()) or ""
            results.append({
                "tax_code": tax_code.strip(),
                "company_name": company_name.strip(),
                "issue_date": issue_date.strip(),
                "detail_url": detail_url,
            })
    return results


async def extract_company_details(page) -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        await page.wait_for_selector('h1', timeout=10000)
        title = await page.locator('h1').text_content()
        if title and ' - ' in title:
            parts = title.split(' - ', 1)
            data['tax_code'] = parts[0].strip()
            data['company_name'] = parts[1].strip()
    except Exception:
        pass
    items = page.locator('li')
    count = await items.count()
    for i in range(count):
        txt = await items.nth(i).text_content()
        if not txt:
            continue
        t = txt.strip()
        if t.startswith('Mã số thuế:'):
            data['tax_code'] = t.replace('Mã số thuế:', '').strip()
        elif t.startswith('Ngày cấp:'):
            data['registration_date'] = t.replace('Ngày cấp:', '').strip()
        elif 'Địa chỉ trụ sở' in t and ':' in t:
            data['address'] = t.split(':', 1)[1].strip()
        elif t.startswith('Đại diện Pháp luật:'):
            data['legal_representative'] = t.replace('Đại diện Pháp luật:', '').strip()
    if 'status' not in data:
        data['status'] = 'Active'
    return data


@activity.defn
async def search_thuvienphapluat_activity(request: dict[str, Any], extraction_folder: str) -> dict[str, Any]:
    term = (request.get("search_term") or "").strip()
    search_type = (request.get("search_type") or "company_name").strip()
    list_mode = bool(request.get("list_mode", False))

    if not term:
        return {"success": False, "message": "search_term required"}

    if not extraction_folder:
        run_id = activity.info().workflow_run_id or "unknown"
        extraction_folder = os.path.join("/tmp", "entity_lookup", f"{run_id}_{term}")
        os.makedirs(extraction_folder, exist_ok=True)

    search_url = build_search_url(term, search_type)

    async with launch_chromium_page(headless=True) as (_, _, page):
        await page.goto(search_url)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=os.path.join(extraction_folder, "01_search.png"), full_page=True)

        results = await extract_search_results(page)
        if not results:
            return {"success": False, "message": f"No companies found for: {term}", "processing_time": 0.0}

        if list_mode:
            return {"success": True, "message": f"Found {len(results)} companies", "companies": results}

        first = results[0]
        if first.get('detail_url'):
            detail_url = first['detail_url']
            if not detail_url.startswith('http'):
                detail_url = f"https://thuvienphapluat.vn{detail_url}"
            await page.goto(detail_url)
            await page.wait_for_timeout(2000)
            details = await extract_company_details(page)
            combined = {**first, **details}
        else:
            combined = first

        await page.screenshot(path=os.path.join(extraction_folder, "02_details.png"), full_page=True)

        return {"success": True, "message": f"Found company for: {term}", "data": combined}


