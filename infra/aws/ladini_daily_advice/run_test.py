import json
from lambda_function import lambda_handler

if __name__ == '__main__':
    print('Invoking lambda_handler...')
    res = lambda_handler({}, None)
    print(json.dumps(res, ensure_ascii=False, indent=2))
