from lambda_function import WeatherSnapshot, generate_advice, Farmer, build_message


def test_generate_and_build():
    # sample weather cases
    w1 = WeatherSnapshot(precipitation_mm=25.0, humidity_pct=70.0, wind_kmh=10.0, etp_mm=1.0)
    advice1 = generate_advice(w1, 'maïs')
    f1 = Farmer(farmer_id='1', phone='+330000000', lat=12.34, lon=56.78, crop='maïs')
    msg1 = build_message(f1, w1, advice1)
    print('---CASE 1---')
    print(advice1)
    print(msg1)

    w2 = WeatherSnapshot(precipitation_mm=0.0, humidity_pct=30.0, wind_kmh=30.0, etp_mm=5.0)
    advice2 = generate_advice(w2, 'riz')
    f2 = Farmer(farmer_id='2', phone='+330000001', lat=1.23, lon=4.56, crop='riz')
    msg2 = build_message(f2, w2, advice2)
    print('\n---CASE 2---')
    print(advice2)
    print(msg2)


if __name__ == '__main__':
    test_generate_and_build()
