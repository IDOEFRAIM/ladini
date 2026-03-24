#!/usr/bin/env python3
import sys
import boto3

def preview(bucket: str, key: str, max_bytes: int = 2000000, chars: int = 4000):
    s3 = boto3.client('s3')
    obj = s3.get_object(Bucket=bucket, Key=key)
    data = obj['Body'].read(max_bytes)
    text = data.decode('utf-8', 'replace')
    print(text[:chars])

def main():
    if len(sys.argv) < 2:
        print('Usage: preview_s3_file.py <s3_key> [bucket]')
        sys.exit(2)
    key = sys.argv[1]
    bucket = sys.argv[2] if len(sys.argv) > 2 else 'ladini-storage'
    preview(bucket, key)

if __name__ == '__main__':
    main()
