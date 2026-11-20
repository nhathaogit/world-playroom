import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
import main
from playroom_catalog import initialize_catalog, list_playrooms
import companion_creator

ROOT = Path(__file__).resolve().parents[1]


class FreshInstallTests(unittest.TestCase):
    def test_bootstrap_is_empty_and_restart_preserves_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'starter', root / 'starter')
            rooms = initialize_catalog(root)
            state_path = rooms / 'sandbox/room_state.json'
            state = json.loads(state_path.read_text(encoding='utf-8'))
            self.assertEqual(state['history'], [])
            self.assertEqual(state['roster'], {})
            self.assertFalse(list_playrooms(rooms)[0]['has_progress'])
            state['history'] = [{'is_player': True, 'text': 'My own turn'}]
            state_path.write_text(json.dumps(state), encoding='utf-8')
            initialize_catalog(root)
            self.assertEqual(json.loads(state_path.read_text())['history'], state['history'])

    def test_market_and_fresh_state_work_through_http(self):
        with TestClient(main.app) as client:
            self.assertEqual(client.get('/').status_code, 200)
            catalog = client.get('/api/playrooms')
            self.assertEqual(catalog.status_code, 200)
            self.assertEqual(catalog.json()['playrooms'][0]['id'], 'sandbox')
            response = client.get('/api/state', headers={'X-Playroom-ID': 'sandbox'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['history'], [])
            self.assertEqual(client.get('/static/companion-placeholder.svg').status_code, 200)

    def test_player_can_create_own_companion_without_any_seed_save(self):
        data = {'room_id': 'my-character', 'name': 'Alex', 'age': 28,
            'description': 'A new character', 'origin': 'The city', 'occupation': 'Artist',
            'appearance': 'Dark jacket', 'personality': 'Direct and curious',
            'voice': 'Casual', 'desire': 'Finish an exhibition', 'relationship': 'New acquaintance',
            'background': 'Alex has a studio.', 'setting': 'A studio',
            'starting_situation': 'Alex hears the door open.',
            'opening': [{'type': 'dialogue', 'text': 'Hello.'}],
            'metrics': [{'label': 'Trust', 'initial': 0}],
            'milestones': [{'at': 0, 'title': 'Acquaintance', 'meaning': 'Just met'}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'boyfriend-blueprint', root / 'boyfriend-blueprint')
            (root/'static').mkdir()
            shutil.copy2(ROOT/'static/companion-placeholder.svg', root/'static/companion-placeholder.svg')
            rooms = root/'playrooms'; rooms.mkdir()
            companion_creator.publish(companion_creator.BoyfriendCreate(**data), rooms, root)
            state = json.loads((rooms/'my-character/room_state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['history'], [])
            self.assertFalse(state['session_started'])


if __name__ == '__main__':
    unittest.main()
