// Region labels match the owner's suppliers/base.py. Expanded wording is for
// display only: category IDs, product IDs and region codes stay unchanged.
const catalogRegionNames = <String, String>{
  'GLOBAL': 'Глобальный',
  'WW': 'Глобальный',
  'CIS': 'СНГ',
  'RU': 'Россия',
  'KZ': 'Казахстан',
  'UA': 'Украина',
  'BY': 'Беларусь',
  'UZ': 'Узбекистан',
  'TJ': 'Таджикистан',
  'KG': 'Киргизия',
  'TR': 'Турция',
  'EU': 'Европа',
  'US': 'США',
  'UK': 'Великобритания',
  'GB': 'Великобритания',
  'DE': 'Германия',
  'PL': 'Польша',
  'FR': 'Франция',
  'IN': 'Индия',
  'ID': 'Индонезия',
  'BD': 'Бангладеш',
  'PH': 'Филиппины',
  'MY': 'Малайзия',
  'SG': 'Сингапур',
  'TH': 'Таиланд',
  'VN': 'Вьетнам',
  'BR': 'Бразилия',
  'LATAM': 'Латинская Америка',
  'MENA': 'Ближний Восток',
  'AE': 'Объединённые Арабские Эмираты',
  'SA': 'Саудовская Аравия',
  'AR': 'Аргентина',
  'MX': 'Мексика',
  'JP': 'Япония',
  'KR': 'Корея',
  'CN': 'Китай',
  'TW': 'Тайвань',
  'ASIA': 'Азия',
  'SEA': 'Юго-Восточная Азия',
  'NA': 'Северная Америка',
};

String catalogDisplayName(String name) => name.replaceAllMapped(
  RegExp(r'\(([A-Za-z]+(?:\s*/\s*[A-Za-z]+)*)\)'),
  (match) {
    final codes = match.group(1)!.split('/').map((s) => s.trim().toUpperCase());
    if (codes.any((code) => !catalogRegionNames.containsKey(code))) {
      return match.group(0)!;
    }
    return '(${codes.map((code) => catalogRegionNames[code]!).join(' / ')})';
  },
);
