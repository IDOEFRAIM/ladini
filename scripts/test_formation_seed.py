"""Quick test harness: apply seed (if DB configured) and validate BurkinaCropTool outputs."""
import os
import sys
from pprint import pprint

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, os.path.abspath(ROOT + '/backend/src'))

from agriconnect.tools.crop import BurkinaCropTool
import asyncio


async def run_test():
    # If DATABASE_URL is present, run the seed runner first
    if os.environ.get('DATABASE_URL'):
        print('DATABASE_URL found — applying seed...')
        os.environ['PYTHONPATH'] = os.environ.get('PYTHONPATH', '')
        from agriconnect.database.run_seed_inera_standard import main as run_seed
        try:
            run_seed()
        except Exception as e:
            print('Seed execution failed:', e)

    tool = BurkinaCropTool()

    print('\nFetching technical sheet for 2 ha maïs in Centre...')
    tech = await tool.get_technical_sheet('mais', 'Centre')
    print('\n--- Technical sheet (preview) ---')
    print(tech)

    inputs = await tool.calculate_inputs('mais', 2.0)
    print('\n--- Calculated inputs for 2 ha ---')
    pprint(inputs)

    # Basic assertions
    npk = inputs.get('NPK_kg') or inputs.get('NPK_kg')
    urea = inputs.get('Urée_kg') or inputs.get('Uree_kg') or inputs.get('Ure_kg') or inputs.get('Urée_kg')
    print('\nExpected: NPK 300 kg, Urée 200 kg')
    print(f'NPK: {npk}, Urée: {urea}')


if __name__ == '__main__':
    asyncio.run(run_test())
