"""Read the game's AMF3 anonymous object of XML records, without executing AS3.

This intentionally accepts only the shape used by Skill.xml.amf: dynamic
anonymous object, string keys, XML values (including AMF string/XML references).
Typed/externalizable objects and all other value types are rejected.
"""
import xml.etree.ElementTree as ET


def read_records(data):
    if len(data) > 32_000_000 or not data.startswith(b'\x0a\x0b\x01'):
        raise ValueError('Expected an anonymous AMF3 XML-record object')
    # XML references share AMF3's object table; slot zero is the root object.
    position, strings, xmls, records = 3, [], [None], {}

    def take(size):
        nonlocal position
        if size < 0 or position + size > len(data):
            raise ValueError('Truncated AMF3 data')
        result = data[position:position + size]
        position += size
        return result

    def u29():
        value = 0
        for index in range(4):
            byte = take(1)[0]
            if index == 3:
                return (value << 8) | byte
            value = (value << 7) | (byte & 127)
            if byte < 128:
                return value
        raise ValueError('Invalid U29')

    def text(table):
        header = u29()
        if not header & 1:
            index = header >> 1
            if index >= len(table) or not isinstance(table[index], str):
                raise ValueError('Unknown AMF3 reference')
            return table[index]
        value = take(header >> 1).decode('utf8')
        if value:
            table.append(value)
        return value

    while True:
        key = text(strings)
        if not key:
            break
        if not key.isascii() or not key.isdecimal() or key in records or len(records) >= 20000:
            raise ValueError('Invalid or duplicate AMF3 record ID')
        if take(1) != b'\x0b':
            raise ValueError('Expected AMF3 XML value')
        xml = text(xmls)
        if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
            raise ValueError('Unsupported XML declarations')
        record = ET.fromstring(xml)
        if record.tag != 'RECORD' or record.findtext('id') != key:
            raise ValueError('AMF3 key/XML ID mismatch')
        records[key] = record
    if position != len(data):
        raise ValueError('Trailing AMF3 data')
    return records
