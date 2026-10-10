import 'package:flutter/material.dart';
import 'ui.dart';

/// Display-only artwork. Product IDs, quantities and prices are never changed.
String? productArtAsset(Map product) {
  if (text(product['kind']) != 'topup') return null;
  final name = '${text(product['title'])} ${text(product['name'])}'
      .toLowerCase();
  // Level-up products may mention diamonds, but aren't a diamond top-up.
  if (RegExp(
    r'level[\s-]*up|прокач|\bevo\b|эволюц|upgrade|апгрейд|уровн',
  ).hasMatch(name)) {
    return null;
  }
  if (RegExp(
    r'voucher|ваучер|membership|weekly|monthly|недел|месяч|\bpass\b|пропуск|booyah|prime|подписк',
  ).hasMatch(name)) {
    return 'assets/products/voucher.webp';
  }
  if (RegExp(r'diamonds?|алмаз|💎').hasMatch(name) ||
      text(product['unit']).toLowerCase() == 'diamond') {
    return 'assets/products/diamonds.webp';
  }
  return null;
}

class ProductArt extends StatelessWidget {
  final Map product;
  final double size;
  const ProductArt(this.product, {super.key, this.size = 56});

  @override
  Widget build(BuildContext context) {
    final asset = productArtAsset(product);
    if (asset == null) return ProductImage(product['image_url'], size: size);
    final tint = asset.endsWith('voucher.webp')
        ? const Color(0xfff59e0b)
        : const Color(0xff06b6d4);
    return ExcludeSemantics(
      child: Container(
        width: size,
        height: size,
        padding: const EdgeInsets.all(3),
        decoration: BoxDecoration(
          color: tint.withValues(alpha: .10),
          borderRadius: BorderRadius.circular(14),
        ),
        child: Image.asset(asset, fit: BoxFit.contain),
      ),
    );
  }
}
