-- Расширение типов каналов публикации: добавление ВКонтакте и Discord
ALTER TABLE channels DROP CONSTRAINT IF EXISTS channels_type_check;
ALTER TABLE channels ADD CONSTRAINT channels_type_check CHECK (type IN ('telegram', 'webhook', 'console', 'vk', 'discord'));
