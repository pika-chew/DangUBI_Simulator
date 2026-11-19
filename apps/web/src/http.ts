/** JSON 오류 응답과 서버/프록시의 일반 텍스트 오류를 구분한다. */
export async function readJSONResponse<T>(response: Response): Promise<T> {
  const raw=await response.text();
  let data: unknown;
  try { data=JSON.parse(raw); }
  catch { throw new Error(response.ok ? "서버 응답이 JSON 형식이 아닙니다. 다시 조회하세요." : `요청을 처리하지 못했습니다(HTTP ${response.status}). 서버 상태를 확인하고 다시 시도하세요.`); }
  if (!response.ok) {
    const detail=data!==null&&typeof data==="object"&&"detail" in data ? (data as {detail:unknown}).detail : null;
    throw new Error(typeof detail==="string" ? detail : Array.isArray(detail) ? detail.map(item=>typeof item==="string"?item:typeof item?.msg==="string"?item.msg:"입력 조건을 확인하세요.").join("\n") : `요청을 처리하지 못했습니다(HTTP ${response.status}).`);
  }
  return data as T;
}
