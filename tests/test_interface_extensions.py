import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from interface_schema_extensions import extend_task_schema


class InterfaceExtensionTests(unittest.TestCase):
    def test_extension_is_narrow_and_does_not_mutate_upstream_schema(self):
        schema=json.loads((ROOT/'deps/tools/interface.schema.json').read_text(encoding='utf8'))
        extended=extend_task_schema(schema)
        self.assertNotIn('default_add',schema['definitions']['taskItem']['properties'])
        task=extended['definitions']['taskItem']
        self.assertEqual(task['properties']['default_add']['type'],['boolean','null'])
        self.assertIs(task['additionalProperties'],False)
        for key in ('default_add','repeatable','repeat_count'):del task['properties'][key]
        self.assertEqual(extended,schema)

    @unittest.skipUnless(importlib.util.find_spec('jsonschema'),'optional schema validation dependency')
    def test_actual_validator_accepts_manual_policy_but_rejects_typos_and_wrong_types(self):
        from jsonschema import Draft7Validator
        for filename in ('interface.schema.json','interface_import.schema.json'):
            schema=extend_task_schema(json.loads((ROOT/'deps/tools'/filename).read_text(encoding='utf8')))
            validator=Draft7Validator(dict(schema,properties=dict(task=schema['properties']['task']),required=[]))
            for policy in (False,True,None):
                self.assertEqual(list(validator.iter_errors(dict(task=[dict(name='stage',entry='Stage',default_add=policy)]))),[])
            for value in (dict(default_add='false'),dict(default_add=0),dict(default_ad=False),dict(repeat_count='2')):
                self.assertTrue(list(validator.iter_errors(dict(task=[dict(name='stage',entry='Stage',**value)]))))


if __name__=='__main__':unittest.main()
