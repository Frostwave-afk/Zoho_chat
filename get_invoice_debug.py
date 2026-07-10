import asyncio
from backend.db.database import AsyncSessionLocal
from backend.services.zoho_service import _headers, get_org_id, settings
import httpx

async def main():
    async with AsyncSessionLocal() as db:
        org_id = await get_org_id(db)
        headers = await _headers(db)
        headers["X-com-zoho-invoice-organizationid"] = org_id
        
        invoice_id = "3929795000000076112" # Vismay Shah's invoice INV-000023
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{settings.zoho_api_base}/invoices/{invoice_id}",
                headers=headers,
                params={"organization_id": org_id},
            )
            data = resp.json()
            print("Invoice JSON keys:", data.keys())
            invoice = data.get("invoice", {})
            print("Customer ID:", invoice.get("customer_id"))
            print("Customer Name:", invoice.get("customer_name"))
            print("Contact Persons in Invoice:", invoice.get("contact_persons"))
            
            # Let's also fetch the customer profile to see its contact persons
            customer_id = invoice.get("customer_id")
            if customer_id:
                c_resp = await client.get(
                    f"{settings.zoho_api_base}/contacts/{customer_id}",
                    headers=headers,
                    params={"organization_id": org_id},
                )
                c_data = c_resp.json()
                contact = c_data.get("contact", {})
                print("Customer Email:", contact.get("email"))
                print("Contact Persons in Customer:", contact.get("contact_persons"))

if __name__ == "__main__":
    asyncio.run(main())
