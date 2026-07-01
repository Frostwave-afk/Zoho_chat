import asyncio
from backend.db.database import get_db
from backend.services.zoho_service import _headers, get_org_id, settings
import httpx

async def main():
    async for db in get_db():
        org_id = await get_org_id(db)
        headers = await _headers(db)
        
        payload = {
            "customer_id": "3929795000000032190", 
            "line_items": [{'name': 'Service', 'description': 'Service', 'rate': 0.0, 'quantity': 1}]
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.zoho_api_base}/invoices",
                headers=headers,
                params={"organization_id": org_id},
                json=payload,
            )
            print("Zero rate response:", resp.status_code, resp.text)
            
        payload2 = {
            "customer_id": "3929795000000032190", 
            "line_items": [{'name': 'null', 'description': 'null', 'rate': 11000.0, 'quantity': 1}]
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.zoho_api_base}/invoices",
                headers=headers,
                params={"organization_id": org_id},
                json=payload2,
            )
            print("Null name response:", resp.status_code, resp.text)
        break

if __name__ == "__main__":
    asyncio.run(main())
