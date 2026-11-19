"""화면/API/실행기의 공통 운영 범위. 계정별 AWS 할당량과 구분한다."""
MAX_VEHICLES = 25000
MAX_DURATION_MS = 30 * 86400000
MAX_MESSAGES = 1000000000000
PROFILES = {
    "small_test": {"max_vehicles":100,"max_duration_ms":3600000,"max_messages":10000,"default_clients":1},
    "operations": {"max_vehicles":MAX_VEHICLES,"max_duration_ms":MAX_DURATION_MS,"max_messages":MAX_MESSAGES,"default_clients":500},
}
