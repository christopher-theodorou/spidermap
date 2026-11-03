<?php
// Stand-in for Zabbix's api_jsonrpc.php, used only by test_gateway.py.
// Knows four sessions: a Super admin, an Admin, a normal user and the guest account.
$in = json_decode(file_get_contents('php://input'), true);
$users = [str_repeat('a', 32) => ['userid' => '1', 'username' => 'Admin',    'type' => 3],
          str_repeat('d', 32) => ['userid' => '4', 'username' => 'netadmin', 'type' => 2],
          str_repeat('b', 32) => ['userid' => '2', 'username' => 'viewer',   'type' => 1],
          str_repeat('c', 32) => ['userid' => '3', 'username' => 'guest',    'type' => 1]];
@file_put_contents(getenv('SM_TEST_DIR') . '/api_calls.log', $in['method'] . "\n", FILE_APPEND);
$sid = $in['params']['sessionid'] ?? '';
echo json_encode(isset($users[$sid]) ? ['jsonrpc' => '2.0', 'result' => $users[$sid], 'id' => 1]
                                     : ['jsonrpc' => '2.0', 'error' => ['code' => -32602, 'message' => 'Session terminated'], 'id' => 1]);
