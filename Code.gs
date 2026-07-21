/**
 * OKVEJ: новые заказы Хорошоп из Gmail -> Telegram-бот на Railway.
 *
 * 1) В Gmail создайте ярлык: OKVEJ_NEW_ORDER
 * 2) Создайте фильтр для писем Хорошоп с темой "Оформлен новый заказ"
 *    и применяйте ярлык OKVEJ_NEW_ORDER.
 * 3) В Script Properties задайте:
 *    WEBHOOK_URL = https://okvejbot-production.up.railway.app/api/horoshop-order
 *    WEBHOOK_SECRET = то же значение, что HOROSHOP_ORDER_WEBHOOK_SECRET в Railway
 * 4) Создайте триггер checkHoroshopOrders каждые 5 минут.
 */

const LABEL_NAME = 'OKVEJ_NEW_ORDER';
const PROCESSED_LABEL_NAME = 'OKVEJ_ORDER_SENT';

function checkHoroshopOrders() {
  const props = PropertiesService.getScriptProperties();
  const webhookUrl = String(props.getProperty('WEBHOOK_URL') || '').trim();
  const secret = String(props.getProperty('WEBHOOK_SECRET') || '').trim();

  if (!webhookUrl || !secret) {
    throw new Error('Set WEBHOOK_URL and WEBHOOK_SECRET in Script Properties');
  }

  const sourceLabel = GmailApp.getUserLabelByName(LABEL_NAME);
  if (!sourceLabel) {
    throw new Error('Gmail label not found: ' + LABEL_NAME);
  }

  let processedLabel = GmailApp.getUserLabelByName(PROCESSED_LABEL_NAME);
  if (!processedLabel) {
    processedLabel = GmailApp.createLabel(PROCESSED_LABEL_NAME);
  }

  const threads = GmailApp.search(
    'label:' + LABEL_NAME + ' -label:' + PROCESSED_LABEL_NAME,
    0,
    20
  );

  threads.reverse().forEach(function(thread) {
    const messages = thread.getMessages();
    const message = messages[messages.length - 1];

    const payload = parseOrderEmail_(message);
    const response = UrlFetchApp.fetch(
      webhookUrl + '?secret=' + encodeURIComponent(secret),
      {
        method: 'post',
        contentType: 'application/json',
        payload: JSON.stringify(payload),
        muteHttpExceptions: true
      }
    );

    const status = response.getResponseCode();
    if (status >= 200 && status < 300) {
      thread.addLabel(processedLabel);
      console.log('Order sent: ' + payload.order_number);
    } else {
      throw new Error(
        'Webhook HTTP ' + status + ': ' + response.getContentText()
      );
    }
  });
}

function parseOrderEmail_(message) {
  const subject = message.getSubject() || '';
  const plainBody = message.getPlainBody() || '';
  const normalized = plainBody
    .replace(/\r/g, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  const orderNumber = firstMatch_(
    subject + '\n' + normalized,
    [
      /(?:заказ|замовлення)\s*(?:№|#)?\s*([A-Za-zА-Яа-яІіЇїЄє0-9_-]{2,})/i,
      /(?:номер|number)\s*(?:заказа|замовлення)?\s*[:№#]?\s*([A-Za-z0-9_-]{2,})/i
    ],
    String(message.getId())
  );

  const customerName = firstMatch_(
    normalized,
    [
      /(?:покупатель|покупець|клиент|клієнт|получатель|одержувач)\s*:\s*(.+)/i,
      /(?:имя|ім['’]?я|name)\s*:\s*(.+)/i
    ],
    ''
  );

  const phone = firstMatch_(
    normalized,
    [
      /(?:телефон|phone)\s*:\s*([+0-9() \-]{7,})/i,
      /(\+?380[\d() \-]{9,})/
    ],
    ''
  );

  const email = firstMatch_(
    normalized,
    [
      /(?:e-?mail|эл\.?\s*почта|ел\.?\s*пошта)\s*:\s*([^\s<>]+@[^\s<>]+)/i,
      /([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})/i
    ],
    ''
  );

  const total = firstMatch_(
    normalized,
    [
      /(?:итого|всього|сумма|сума|total)\s*:\s*([\d\s.,]+)\s*(?:грн|₴|UAH)?/i,
      /([\d\s.,]+)\s*(?:грн|₴|UAH)\s*(?:итого|всього)?/i
    ],
    ''
  );

  const delivery = firstMatch_(
    normalized,
    [
      /(?:доставка|delivery)\s*:\s*(.+)/i,
      /(?:способ доставки|спосіб доставки)\s*:\s*(.+)/i
    ],
    ''
  );

  const payment = firstMatch_(
    normalized,
    [
      /(?:оплата|payment)\s*:\s*(.+)/i,
      /(?:способ оплаты|спосіб оплати)\s*:\s*(.+)/i
    ],
    ''
  );

  const address = firstMatch_(
    normalized,
    [
      /(?:адрес|адреса|отделение|відділення|warehouse)\s*:\s*(.+)/i
    ],
    ''
  );

  return {
    id: orderNumber,
    order_number: orderNumber,
    customer_name: cleanLine_(customerName),
    phone: cleanLine_(phone),
    email: cleanLine_(email),
    total: cleanLine_(total),
    delivery: cleanLine_(delivery),
    payment: cleanLine_(payment),
    delivery_address: cleanLine_(address),
    comment: normalized.substring(0, 3500),
    source: 'horoshop_email',
    email_subject: subject,
    email_message_id: String(message.getId()),
    received_at: message.getDate().toISOString()
  };
}

function firstMatch_(text, patterns, fallback) {
  for (let i = 0; i < patterns.length; i++) {
    const match = text.match(patterns[i]);
    if (match && match[1]) {
      return match[1];
    }
  }
  return fallback;
}

function cleanLine_(value) {
  return String(value || '').split('\n')[0].trim();
}

/**
 * Одноразовый тест. Сначала задайте Script Properties.
 */
function testWebhook() {
  const props = PropertiesService.getScriptProperties();
  const webhookUrl = String(props.getProperty('WEBHOOK_URL') || '').trim();
  const secret = String(props.getProperty('WEBHOOK_SECRET') || '').trim();

  const response = UrlFetchApp.fetch(
    webhookUrl + '?secret=' + encodeURIComponent(secret),
    {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({
        id: 'TEST-' + new Date().getTime(),
        order_number: 'TEST',
        customer_name: 'Тест OKVEJ',
        phone: '+380000000000',
        total: '100',
        delivery: 'Тестова доставка',
        payment: 'Тестова оплата',
        comment: 'Перевірка передачі замовлення з Gmail у Telegram.'
      }),
      muteHttpExceptions: true
    }
  );

  console.log(response.getResponseCode());
  console.log(response.getContentText());
}
