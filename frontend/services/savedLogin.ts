import { del, get, post } from '../lib/request';

export const readSavedLogin = () => get<{ available: boolean; saved: boolean; username?: string; password?: string }>('/desktop/credentials');
export const saveLogin = (username: string, password: string) => post('/desktop/credentials', { username, password });
export const forgetLogin = () => del('/desktop/credentials');
