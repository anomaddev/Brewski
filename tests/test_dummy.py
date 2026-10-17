from brewski.sources.dummy import DummySource


def test_heaters_raise_temperature():
    source = DummySource(noise=False)
    start = source.read_temperatures(1)[0]
    source.set_heaters([True, True])
    source.step(30)
    assert source.read_temperatures(1)[0] > start + 10


def test_temperature_falls_when_heaters_are_off():
    source = DummySource(noise=False, initial_temps=(150.0, 140.0))
    source.set_heaters([False, False])
    source.step(30)
    assert source.read_temperatures(1)[0] < 150


def test_second_sensor_stays_cooler_while_heating():
    source = DummySource(noise=False)
    source.set_heaters([True, True])
    source.step(60)
    boiler, column = source.read_temperatures(2)
    assert column < boiler


def test_one_heater_is_slower_than_two():
    one = DummySource(noise=False)
    two = DummySource(noise=False)
    one.set_heaters([True])
    two.set_heaters([True, True])
    one.step(60)
    two.step(60)
    assert two.read_temperatures(1)[0] > one.read_temperatures(1)[0]
