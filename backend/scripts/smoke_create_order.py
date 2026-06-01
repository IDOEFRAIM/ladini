import asyncio
from agriconnect.services.database.database_service import AgriDatabaseService

async def run():
    svc = AgriDatabaseService()
    producer_id = "3cadb350-59e5-4ad8-ae95-5ff7cc1350bd"
    print('Creating product...')
    try:
        res = await svc.create_product(producer_id=producer_id, name='TestProd', price=1000, quantity_for_sale=10, unit='KG')
        print('create_product ->', res)
    except Exception as e:
        print('create_product error:', type(e), e)

    # If product created, try create_order against first product id
    try:
        prods = await svc.list_products(producer_id)
        print('list_products ->', prods)
        if prods:
            pid = prods[0].get('id') or prods[0].get('product_id') or prods[0].get('id')
            print('Attempting create_order for product id', pid)
            order = await svc.create_order(product_id=pid, quantity=1, buyer_phone='000000000')
            print('create_order ->', order)
        else:
            print('No products to order')
    except Exception as e:
        print('create_order path error:', type(e), e)

if __name__ == '__main__':
    asyncio.run(run())
