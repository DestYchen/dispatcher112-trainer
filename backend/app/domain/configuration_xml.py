"""Strict XML exchange for the public operational configuration, without secrets."""

from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

from app.domain.runtime_configuration import RuntimeConfiguration


def encode(value: RuntimeConfiguration) -> bytes:
    root = Element("runtime_configuration", {"schema": "1"})
    for group, values in value.model_dump().items():
        section = SubElement(root, group)
        for key, setting in values.items():
            SubElement(section, key).text = (
                str(setting).lower() if isinstance(setting, bool) else str(setting)
            )
    result = tostring(root, encoding="utf-8", xml_declaration=True)
    assert isinstance(result, bytes)
    return result


def decode(raw: bytes) -> RuntimeConfiguration:
    if len(raw) > 65536:
        raise ValueError("Configuration XML exceeds 64 KiB")
    text = raw.decode("utf-8-sig")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("DTD and entities are not allowed")
    root = fromstring(text)
    expected = RuntimeConfiguration().model_dump()
    if root.tag != "runtime_configuration" or root.attrib != {"schema": "1"}:
        raise ValueError("Unsupported XML configuration schema")
    if len(root) != len(expected) or {section.tag for section in root} != set(expected):
        raise ValueError("Configuration sections are missing or duplicated")
    parsed = {}
    for section in root:
        fields = expected[section.tag]
        if (
            section.attrib
            or len(section) != len(fields)
            or {item.tag for item in section} != set(fields)
        ):
            raise ValueError("Configuration fields are missing or duplicated")
        values: dict[str, bool | int | str] = {}
        for item in section:
            if item.attrib or len(item) or item.text is None:
                raise ValueError("Configuration fields must contain scalar values")
            value = item.text.strip()
            if type(fields[item.tag]) is bool:
                if value not in {"true", "false"}:
                    raise ValueError("Use true or false for boolean fields")
                values[item.tag] = value == "true"
            elif type(fields[item.tag]) is int:
                values[item.tag] = int(value)
            else:
                values[item.tag] = value
        parsed[section.tag] = values
    return RuntimeConfiguration.model_validate(parsed)
