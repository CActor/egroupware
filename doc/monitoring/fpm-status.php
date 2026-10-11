<?php
/**
 * EGroupware performance monitoring: read the PHP-FPM status page
 *
 * Minimal FastCGI client, so the status page does not need to be published via
 * the webserver. Run inside the EGroupware container, e.g.:
 *
 *   docker exec -i egroupware php < fpm-status.php
 *
 * Prints the status as one line of JSON, or nothing (exit code 1) on error.
 *
 * @link https://www.egroupware.org
 * @license http://opensource.org/licenses/gpl-license.php GPL - GNU General Public License
 */

$host = getenv('EGW_FPM_STATUS_HOST') ?: '127.0.0.1';
$port = (int)(getenv('EGW_FPM_STATUS_PORT') ?: 9001);
$path = '/fpm-status';

/**
 * Encode a FastCGI record
 */
function fcgi_record(int $type, string $content, int $id = 1) : string
{
	return pack('CCnnCx', 1, $type, $id, strlen($content), 0).$content;
}

/**
 * Encode a FastCGI name-value pair (all our names and values are < 128 bytes)
 */
function fcgi_pair(string $name, string $value) : string
{
	return chr(strlen($name)).chr(strlen($value)).$name.$value;
}

if (!($sock = @stream_socket_client("tcp://$host:$port", $errno, $errstr, 2)))
{
	exit(1);
}
stream_set_timeout($sock, 5);

$params = '';
foreach([
	'GATEWAY_INTERFACE' => 'FastCGI/1.0',
	'REQUEST_METHOD'    => 'GET',
	'SCRIPT_NAME'       => $path,
	'SCRIPT_FILENAME'   => $path,
	'REQUEST_URI'       => $path.'?json',
	'QUERY_STRING'      => 'json',
	'SERVER_PROTOCOL'   => 'HTTP/1.1',
] as $name => $value)
{
	$params .= fcgi_pair($name, $value);
}
// FCGI_BEGIN_REQUEST (role responder, no keep-alive), FCGI_PARAMS, FCGI_STDIN
fwrite($sock, fcgi_record(1, pack('nCx5', 1, 0)).
	fcgi_record(4, $params).fcgi_record(4, '').fcgi_record(5, ''));

$stdout = '';
while (strlen($header = fread($sock, 8)) === 8)
{
	$h = unpack('Cversion/Ctype/nid/nlength/Cpadding', $header);
	$content = '';
	while (strlen($content) < $h['length']+$h['padding'] && !feof($sock))
	{
		$content .= fread($sock, $h['length']+$h['padding']-strlen($content));
	}
	if ($h['type'] === 6)	// FCGI_STDOUT
	{
		$stdout .= substr($content, 0, $h['length']);
	}
	elseif ($h['type'] === 3)	// FCGI_END_REQUEST
	{
		break;
	}
}
fclose($sock);

// strip the CGI headers
$body = ($pos = strpos($stdout, "\r\n\r\n")) !== false ? substr($stdout, $pos+4) : '';
if (!is_array(json_decode($body, true)))
{
	exit(1);
}
echo $body, "\n";
